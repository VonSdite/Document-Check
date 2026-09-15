from dataclasses import dataclass


@dataclass(frozen=True)
class UserIdentity:
    subject: str
    display_name: str
    source: str
    ip: str
    avatar: str = ""
    employee_number: str = ""
    profile_version: int = 0

    @property
    def label(self) -> str:
        name = self.display_name or subject_label(self.subject)
        if self.employee_number and self.employee_number != name:
            return f"{name}（{self.employee_number}）"
        return name


def ip_subject(ip: str) -> str:
    return f"ip:{str(ip or '0.0.0.0').strip() or '0.0.0.0'}"


def subject_label(subject: str) -> str:
    subject = str(subject or "").strip()
    if subject.startswith("ip:"):
        return subject[3:]
    if subject.startswith("cookie_session:"):
        return subject[15:]
    return subject
