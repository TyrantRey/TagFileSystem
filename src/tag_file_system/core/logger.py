# Code by AkinoAlice@TyrantRey

import logging
from sys import stdout

from tag_file_system.config import LoggingSetting

logging_setting = LoggingSetting()
logger = logging.getLogger(name=__name__)

logging.basicConfig(
    level=logging_setting.log_level,
    filename=logging_setting.log_file,
    filemode=logging_setting.filemode,
    format="[%(levelname)s] - %(asctime)s - %(message)s - %(pathname)s:%(lineno)d",
)
handler = logging.StreamHandler(stream=stdout)
logger.addHandler(handler)
