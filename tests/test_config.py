# Code by AkinoAlice@TyrantRey

from pathlib import Path

import pytest

from tag_file_system.config import Config, ConfigError, DaemonConfig, LoggingConfig


def test_defaults_match_design():
    config = Config()

    assert config.logging == LoggingConfig(level="INFO", file=Path(".tfs/tag_file_system.log"))
    assert config.daemon == DaemonConfig(
        bind="127.0.0.1", port=7411, stop_timeout_seconds=30, run_warn_after_seconds=300
    )
    assert config.remotes == {}


def test_loads_full_file():
    config = Config.loads(
        """
        [logging]
        level = "debug"
        file = "logs/tfs.log"

        [daemon]
        bind = "0.0.0.0"
        port = 8080
        stop_timeout_seconds = 5
        run_warn_after_seconds = 1.5

        [remotes]
        photos = "/home/photo"
        "odd name" = "D:\\\\backup"
        """
    )

    assert config.logging.level == "DEBUG"
    assert config.logging.file == Path("logs/tfs.log")
    assert config.daemon.bind == "0.0.0.0"
    assert config.daemon.port == 8080
    assert config.daemon.stop_timeout_seconds == 5
    assert config.daemon.run_warn_after_seconds == 1.5
    # remotes are kept verbatim: the file is shared between hosts
    assert config.remotes == {"photos": "/home/photo", "odd name": "D:\\backup"}


def test_missing_sections_fall_back_to_defaults():
    config = Config.loads('[remotes]\nx = "/x"\n')

    assert config.logging == LoggingConfig()
    assert config.daemon == DaemonConfig()
    assert config.remotes == {"x": "/x"}
    assert Config.loads("") == Config()


@pytest.mark.parametrize(
    "text",
    [
        "[logging]\nlevel = 'LOUD'\n",  # unknown level
        "[logging]\nlevel = 10\n",
        "[logging]\nfile = '/var/log/tfs.log'\n",  # absolute, POSIX
        "[logging]\nfile = 'C:\\\\tfs.log'\n",  # absolute, Windows
        "[logging]\nfile = '\\\\\\\\server\\\\share\\\\x.log'\n",  # UNC
        "[logging]\nfile = 'c:x'\n",  # drive-relative
        "[logging]\nfile = 'D:'\n",
        "[logging]\nfile = 'c:..\\\\x'\n",
        "[logging]\nfile = ''\n",
        "[logging]\nfile = '.'\n",
        "[logging]\nfile = '../escape.log'\n",
        "[logging]\nfile = '.tfs/../../escape.log'\n",
        "[daemon]\nport = 0\n",
        "[daemon]\nport = 70000\n",
        "[daemon]\nport = '80'\n",  # TOML is typed: no coercion
        "[daemon]\nport = true\n",
        "[daemon]\nport = 80.0\n",
        "[daemon]\nstop_timeout_seconds = '30'\n",
        "[daemon]\nstop_timeout_seconds = -1\n",
        "[daemon]\nstop_timeout_seconds = inf\n",
        "[daemon]\nrun_warn_after_seconds = 0\n",
        "[daemon]\nrun_warn_after_seconds = true\n",
        "[daemon]\nrun_warn_after_seconds = nan\n",
        "[daemon]\nbind = ''\n",
        "[daemon]\nbind = 'not an ip'\n",
        "[daemon]\nbind = 127\n",
        "[daemon]\nprot = 1\n",  # typo -> unknown key
        "[logging]\nlevle = 'INFO'\n",
        "[remotes]\nphotos = ''\n",
        "[remotes]\nphotos = 'relative/dir'\n",  # remotes are outside the root
        "[remotes]\nphotos = '.'\n",
        "[remotes]\nphotos = 1\n",
        "[remotes]\nphotos = ['/a']\n",
        "[remotes.photos]\npath = '/a'\n",
        "bogus = 1\n",
        "[logging\nlevel = 'INFO'",  # not TOML
    ],
)
def test_invalid_config_is_a_config_error(text: str):
    with pytest.raises(ConfigError):
        Config.loads(text)


def test_numbers_of_the_right_type_are_accepted():
    config = Config.loads("[daemon]\nport = 80\nstop_timeout_seconds = 30\nrun_warn_after_seconds = 2.5\n")

    assert config.daemon.port == 80
    assert config.daemon.stop_timeout_seconds == 30.0
    assert config.daemon.run_warn_after_seconds == 2.5
    assert Config.loads("[daemon]\nbind = '::1'\n").daemon.bind == "::1"


def test_dump_load_roundtrip(tmp_path: Path):
    config = Config(
        logging=LoggingConfig(level="WARNING", file=Path("logs/x.log")),
        daemon=DaemonConfig(bind="0.0.0.0", port=9000, stop_timeout_seconds=2.5),
        remotes={
            "photos": "/home/photo",
            "with space": 'C:\\a "b"',
            "unicode": "/путь/名前",
            "control": "/a\tb\nc\x01d",
        },
    )
    path = tmp_path / "config.toml"

    config.write(path)

    assert Config.load(path) == config
    assert Config.loads(Config().dumps()) == Config()
    # POSIX targets are written as typed, on every host
    assert 'photos = "/home/photo"' in path.read_text(encoding="utf-8")


def test_load_missing_file_is_a_config_error(tmp_path: Path):
    with pytest.raises(ConfigError):
        Config.load(tmp_path / "nope.toml")
    with pytest.raises(ConfigError):
        Config.load(tmp_path)  # a directory


def test_load_handles_bom_and_rejects_non_utf8(tmp_path: Path):
    bom = tmp_path / "bom.toml"
    bom.write_bytes(b"\xef\xbb\xbf[daemon]\nport = 1\n")
    assert Config.load(bom).daemon.port == 1

    bad = tmp_path / "bad.toml"
    bad.write_bytes(b"\xff\xfe[logging]\n")
    with pytest.raises(ConfigError):
        Config.load(bad)
