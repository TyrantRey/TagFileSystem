# Code by AkinoAlice@TyrantRey

import re

from pydantic import BaseModel, Field, field_validator, model_validator


def normalize_tag(raw: str) -> str:
    tag = raw.lower()
    tag = re.sub(r"[^\w\-]", "", tag, flags=re.UNICODE)
    tag = re.sub(r"-+", "-", tag)
    tag = tag.strip("-")
    return tag


class Tag(BaseModel):
    name: str
    category: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Tag name cannot be empty")

        if ":" in v:
            parts = v.split(":", 1)
            v = parts[1].strip()

        normalized = normalize_tag(v)

        if not normalized:
            raise ValueError(f"Tag name '{v}' is invalid after normalization")

        return normalized

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"Tag('{self.name}')"


class TagAction(BaseModel):
    name: str
    params: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def parse_name_and_params(cls, values):
        if isinstance(values, dict):
            name = values.get("name", "")
        else:
            name = str(values)

        if not name or not name.strip():
            raise ValueError("Action name cannot be empty")

        name = name.strip().lower()

        if ":" in name:
            parts = name.split(":", 1)
            action_name = parts[0].strip()
            params_str = parts[1]

            params = {}
            for param in params_str.split(","):
                param = param.strip()
                if "=" in param:
                    key, value = param.split("=", 1)
                    params[key.strip()] = value.strip()

            return {"name": action_name, "params": params}

        return {"name": name, "params": {}}

    def __str__(self) -> str:
        if self.params:
            params_str = ", ".join(f"{k}={v}" for k, v in self.params.items())
            return f"@@{self.name}({params_str})"
        return f"@@{self.name}"

    def __repr__(self) -> str:
        if self.params:
            return f"TagAction('{self.name}', {self.params})"
        return f"TagAction('{self.name}')"


class TagParserOutput(BaseModel):
    tags: list[Tag]
    actions: list[TagAction]
