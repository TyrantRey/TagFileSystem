# Code by AkinoAlice@TyrantRey

"""Per-folder ``.tfsfunctions.yaml`` configuration (DESIGN/v0-4-0.md §3–§4):
which handlers apply to a folder and everything below it, with what
parameters, minus what each entry excludes."""

from tag_file_system.functions.model import (
    PREDICATE_KEYS,
    SUPPORTED_VERSION,
    AllOf,
    AnyOf,
    Entry,
    Exclude,
    FilenameMatches,
    FunctionsError,
    FunctionsFile,
    Not,
    Predicate,
    Subject,
    TagIs,
    display,
    file_label,
    load_file,
    parse_predicate,
)
from tag_file_system.functions.store import (
    Applied,
    Effective,
    FunctionsStore,
    LoadReport,
    Suppressed,
    chain,
    explain_payload,
    folder_of,
    functions_payload,
)

__all__ = [
    "PREDICATE_KEYS",
    "SUPPORTED_VERSION",
    "AllOf",
    "AnyOf",
    "Applied",
    "Effective",
    "Entry",
    "Exclude",
    "FilenameMatches",
    "FunctionsError",
    "FunctionsFile",
    "FunctionsStore",
    "LoadReport",
    "Not",
    "Predicate",
    "Subject",
    "Suppressed",
    "TagIs",
    "chain",
    "display",
    "explain_payload",
    "file_label",
    "folder_of",
    "functions_payload",
    "load_file",
    "parse_predicate",
]
