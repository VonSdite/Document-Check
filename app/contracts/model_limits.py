"""用户模型的单次输出容量。"""

MODEL_OUTPUT_TOKENS_DEFAULT = 32768
MODEL_OUTPUT_TOKENS_MIN = 512
MODEL_OUTPUT_TOKENS_MAX = 262144


def model_output_tokens(value=None) -> int:
    if value is None or value == "":
        return MODEL_OUTPUT_TOKENS_DEFAULT
    try:
        result = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("每次回复 token 上限必须是整数。") from exc
    if not MODEL_OUTPUT_TOKENS_MIN <= result <= MODEL_OUTPUT_TOKENS_MAX:
        raise ValueError(
            f"每次回复 token 上限需在 {MODEL_OUTPUT_TOKENS_MIN}-{MODEL_OUTPUT_TOKENS_MAX} 之间。"
        )
    return result
