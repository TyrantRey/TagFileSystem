# Code by AkinoAlice@TyrantRey

from watchfiles import Change
from tab_file_system.core.interface.database import DatabaseOperation

type AllowedOperation = DatabaseOperation | Change

operation_mapping: dict[Change, list[AllowedOperation]] = {
    Change.added: [DatabaseOperation.INSERT, Change.added],
    Change.modified: [DatabaseOperation.UPDATE, Change.modified],
    Change.deleted: [DatabaseOperation.DELETE, Change.deleted],
}
