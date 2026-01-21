from enum import StrEnum

class Role(StrEnum):
    USER = "user"
    ADMIN = "admin"
    BOSS = "boss"

class Status(StrEnum):
    NEW = "NEW"
    ACCEPTED = "ACCEPTED"
    IN_PROGRESS = "IN_PROGRESS"
    CLOSED = "CLOSED"
    DRAFT = "DRAFT"

class Priority(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
