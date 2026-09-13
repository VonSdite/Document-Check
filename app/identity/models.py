from dataclasses import dataclass


@dataclass(frozen=True)
class UserIdentity:
    subject: str
    display_name: str
    source: str
    ip: str

    @property
    def label(self) -> str:
        return self.display_name or subject_label(self.subject)


def ip_subject(ip: str) -> str:
    return f"ip:{str(ip or '0.0.0.0').strip() or '0.0.0.0'}"


def subject_label(subject: str) -> str:
    subject = str(subject or "").strip()
    if subject.startswith("ip:"):
        return subject[3:]
    if subject.startswith("trusted_header:"):
        return subject[15:]
    if subject.startswith("saml:"):
        return subject[5:]
    return subject
