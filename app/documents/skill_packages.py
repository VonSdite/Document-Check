"""解析 Skill 文件包中的指令与参考资料，生成模型检查内容。"""

import hashlib
import io
import json
import lzma
import os
import re
import stat
import tempfile
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml
from flask import current_app

from app.documents.extraction import ALLOWED_EXTENSIONS, extract_text

MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
MAX_EXPANDED_BYTES = 20 * 1024 * 1024
MAX_PACKAGE_FILES = 64
MAX_PROMPT_CHARS = 200_000
TEXT_EXTENSIONS = {"md", "txt", "json", "yaml", "yml", "csv", "tsv", "rst"}
REFERENCE_EXTENSIONS = TEXT_EXTENSIONS | ALLOWED_EXTENSIONS


@dataclass(frozen=True)
class SkillPackage:
    prompt: str
    metadata: dict
    archive: bytes


def skill_metadata(raw):
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def skill_archive_path(digest):
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("Skill 包版本无效。")
    return (
        Path(current_app.config["ROOT_DIR"]) / "instance" / "skills" / f"{digest}.zip"
    )


def store_skill_package(package):
    destination = skill_archive_path(package.metadata["sha256"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, suffix=".tmp", delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(package.archive)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_skill_package(upload):
    filename = str(upload.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    if any(ord(char) < 32 for char in filename) or len(filename) > 255:
        raise ValueError("Skill 文件包名称无效。")
    if Path(filename).suffix.lower() not in {".zip", ".skill"}:
        raise ValueError("请上传 ZIP 格式的 .zip 或 .skill 文件包。")
    archive = upload.stream.read(MAX_ARCHIVE_BYTES + 1)
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise ValueError("Skill 文件包最大为 10 MB。")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            entries = _package_entries(bundle)
            root, entrypoint = _skill_root(entries)
            files = []
            contents = []
            metadata = {}
            body = ""
            total_chars = 0
            for name, entry in sorted(entries.items()):
                relative = name[len(root) :]
                extension = PurePosixPath(relative).suffix.lstrip(".").lower()
                if extension not in REFERENCE_EXTENSIONS:
                    raise ValueError(
                        f"{relative} 不是支持的参考资料。请使用文本、PDF、Word、Excel 或 HTML 文件。"
                    )
                with bundle.open(entry) as source:
                    data = source.read(entry.file_size + 1)
                text = _reference_text(relative, data, extension)
                if not text.strip():
                    raise ValueError(
                        f"{relative} 未读取到文字，请提供可提取文字的资料。"
                    )
                total_chars += len(text)
                if total_chars > MAX_PROMPT_CHARS:
                    raise ValueError(
                        "Skill 指令和参考资料合计最多为 20 万字，请精简文件包。"
                    )
                files.append(
                    {
                        "path": relative,
                        "chars": len(text),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )
                if name == entrypoint:
                    metadata, body = _skill_instructions(text)
                else:
                    contents.append(
                        f"--- 参考资料：{relative} ---\n{text}\n--- 资料结束 ---"
                    )
    except (
        zipfile.BadZipFile,
        zlib.error,
        lzma.LZMAError,
        NotImplementedError,
        RuntimeError,
        EOFError,
        OSError,
    ) as exc:
        raise ValueError(
            "Skill 文件包无法读取，请检查 ZIP 文件是否完整且未加密。"
        ) from exc
    prompt = (
        "按照下方 SKILL.md 的检查步骤与参考资料检查待检查文档。"
        "参考资料用于提供检查标准，问题证据应引用待检查文档。"
        "包内资料已展开为文字，请直接使用这些内容完成检查。"
        "检查结果使用系统规定的中文结构化报告格式。\n\n"
        f"--- SKILL.md ---\n{body}\n--- SKILL.md 结束 ---\n\n" + "\n\n".join(contents)
    )
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError("Skill 检查内容最多为 20 万字，请精简文件包。")
    return SkillPackage(
        prompt=prompt,
        archive=archive,
        metadata={
            "format": 1,
            "filename": filename,
            "sha256": hashlib.sha256(archive).hexdigest(),
            "name": metadata.get("name", ""),
            "description": metadata.get("description", ""),
            "entrypoint": entrypoint[len(root) :],
            "files": files,
            "chars": len(prompt),
        },
    )


def _package_entries(bundle):
    entries = {}
    seen = set()
    total_size = 0
    if len(bundle.infolist()) > MAX_PACKAGE_FILES * 2:
        raise ValueError("Skill 文件包最多包含 64 个文件。")
    for entry in bundle.infolist():
        name = entry.orig_filename.replace("\\", "/")
        parts = name.rstrip("/").split("/")
        if (
            not name
            or name.startswith("/")
            or any(part in {"", ".", ".."} for part in parts)
            or any(ord(char) < 32 or char == ":" for char in name)
        ):
            raise ValueError("Skill 文件包包含无效路径。")
        mode = entry.external_attr >> 16
        if stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise ValueError("Skill 文件包仅支持普通文件和目录。")
        if entry.flag_bits & 1:
            raise ValueError("请上传未加密的 Skill 文件包。")
        total_size += entry.file_size
        if total_size > MAX_EXPANDED_BYTES:
            raise ValueError("Skill 文件包解压后最大为 20 MB。")
        if (
            entry.is_dir()
            or "__MACOSX" in parts
            or parts[-1] in {".DS_Store", "Thumbs.db"}
        ):
            continue
        key = name.casefold()
        if key in seen:
            raise ValueError(f"Skill 文件包存在重名文件：{name}。")
        seen.add(key)
        entries[name] = entry
        if len(entries) > MAX_PACKAGE_FILES:
            raise ValueError("Skill 文件包最多包含 64 个文件。")
    return entries


def _skill_root(entries):
    entrypoints = [
        name for name in entries if PurePosixPath(name).name.casefold() == "skill.md"
    ]
    if len(entrypoints) != 1:
        raise ValueError(
            "文件包需要包含唯一的 SKILL.md，放在根目录或一个顶层文件夹中。"
        )
    entrypoint = entrypoints[0]
    if len(PurePosixPath(entrypoint).parts) > 2:
        raise ValueError("SKILL.md 请放在文件包根目录或一个顶层文件夹中。")
    root = entrypoint[: -len(PurePosixPath(entrypoint).name)]
    if any(not name.startswith(root) for name in entries):
        raise ValueError("请将所有参考资料放在 SKILL.md 所在目录内。")
    return root, entrypoint


def _reference_text(name, data, extension):
    if extension in TEXT_EXTENSIONS:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{name} 请使用 UTF-8 编码。") from exc
        if "\x00" in text:
            raise ValueError(f"{name} 不是有效的文本文件。")
        return text.replace("\r\n", "\n").replace("\r", "\n").strip()
    try:
        if extension in {"docx", "xlsx", "xlsm"}:
            with zipfile.ZipFile(io.BytesIO(data)) as office:
                if (
                    sum(item.file_size for item in office.infolist())
                    > MAX_EXPANDED_BYTES
                ):
                    raise ValueError("Office 文件展开后超过 20 MB。")
        with tempfile.NamedTemporaryFile(
            suffix=f".{extension}", delete=False
        ) as output:
            path = Path(output.name)
            output.write(data)
        try:
            return extract_text(path, extension).strip()
        finally:
            path.unlink(missing_ok=True)
    except Exception as exc:
        raise ValueError(f"参考资料 {name} 读取失败，请检查文件格式和内容。") from exc


def _skill_instructions(text):
    metadata = {}
    body = text
    if text.startswith("---\n"):
        match = re.match(r"\A---\n(.*?)\n---(?:\n|\Z)(.*)\Z", text, re.DOTALL)
        if not match:
            raise ValueError("SKILL.md 的 YAML 元数据需要以 --- 正确结束。")
        try:
            value = yaml.safe_load(match[1])
        except yaml.YAMLError as exc:
            raise ValueError("SKILL.md 的 YAML 元数据格式无效。") from exc
        if value is not None and not isinstance(value, dict):
            raise ValueError("SKILL.md 的 YAML 元数据应为名称、说明等字段。")
        for key in ("name", "description"):
            content = (value or {}).get(key, "")
            if not isinstance(content, str) or len(content) > 2000:
                raise ValueError(f"SKILL.md 的 {key} 需要是 2000 字以内的文本。")
            metadata[key] = content.strip()
        body = match[2].strip()
    if not body.strip():
        raise ValueError("SKILL.md 需要包含具体检查指令。")
    return metadata, body
