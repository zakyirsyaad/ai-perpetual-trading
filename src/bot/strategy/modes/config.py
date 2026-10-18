"""Strict loader for ``config/modes.yaml``, where every number of the LONG module lives.

The file is the manager's configuration block (Indonesian keys, kept as written) plus the keys
marked ``# ditambahkan 2026-10-17``. A lock file binds its sha256, so the loader refuses instead
of guessing: an unknown key, a missing key, a key written twice, a value of the wrong type or a
value out of range raises ``ModesConfigError``, and the message starts with the full YAML key
path (``umum.extra``, ``mode.A_pullback_long.sl_persen``) so a hand-edited typo can be found.

What is accepted:

* Percent fields stay in percent (``tp_persen: 0.40`` is 0.40 %), exactly as written.
* A whole number is accepted, and returned as a float, where a float is expected. A ``bool`` is
  never a number, and a float is never a whole number.
* A number is written in plain decimals (``15``, ``-150``, ``0.035``). What YAML 1.1 would read as
  another number stays text and is refused as the wrong type: a leading zero (``015`` is octal
  13), ``0x`` and ``0b`` literals, base 60 (``1:30`` is 90) and underscores (``25_000``).
* A merge key (``<<``) is refused: it would set keys a second time.
* Dates are quoted strings such as ``"2025-08-01T00:00:00+07:00"`` (YAML turns an unquoted one
  into a datetime of its own). They must be midnight in Asia/Jakarta, because the data is stored
  per WIB day, and they come back as aware datetimes in that zone.

This module reads the file and nothing else: it takes no bars and imports neither the Pine
indicators nor ``research``.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from bot.core.entry_window import parse_weekly_window

__all__ = [
    "BreakoutConfig",
    "GeneralConfig",
    "ModesConfig",
    "ModesConfigError",
    "NearMeanConfig",
    "PairConfig",
    "PullbackConfig",
    "SelectorConfig",
    "SidewaysConfig",
    "TestConfig",
    "config_sha256",
    "load_modes_config",
    "load_modes_config_with_sha256",
    "parse_modes_config",
]

_WIB_ZONE = "Asia/Jakarta"
_WIB = timezone(timedelta(hours=7), "WIB")  # Asia/Jakarta has no daylight saving time
_PAIRS = ("BTC-USDT", "ETH-USDT")
_AUTOMATIC = "otomatis"
_INSTANT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}[+-][0-9]{2}:[0-9]{2}")
_TOP_LEVEL = ("umum", "pemilih_mode", "pair", "mode", "uji")
_MODES = (
    "A_pullback_long",
    "B_breakout_long",
    "C_sideways",
    "D_short_sekarang",
    "D2_pullback_short",
)


class ModesConfigError(ValueError):
    """The configuration is malformed; the message starts with the YAML key path at fault."""


@dataclass(frozen=True)
class GeneralConfig:
    """Block ``umum``: costs, limits and guards shared by every mode."""

    zona_waktu: str
    fee_taker_persen: float
    slippage_persen: float
    maks_posisi_total: int
    maks_posisi_per_pair: int
    maks_entry_per_hari_total: int
    maks_entry_per_hari_per_pair: int
    jeda_menit: int
    penjaga_volatilitas_kali_median_atr: float
    ai_boleh_buka_tutup: bool
    notional_usdt: float
    jendela_entry_mingguan: str


@dataclass(frozen=True)
class SelectorConfig:
    """Block ``pemilih_mode``: the periods and the sideways band of the mode selector."""

    sma_cepat_1h: int
    sma_lambat_1h: int
    ema_4h: int
    sideways_jarak_kali_atr: float


@dataclass(frozen=True)
class PairConfig:
    """One entry of ``pair``. ``faktor_k`` is None for ``otomatis``, and
    ``harus_sama_kondisi_dengan`` is None when the file leaves the key out."""

    aktif: bool
    faktor_k: float | None
    harus_sama_kondisi_dengan: str | None = None


@dataclass(frozen=True)
class PullbackConfig:
    """Modes A and D2. The YAML's ``maks_jarak_di_atas_...`` (A) and ``maks_jarak_di_bawah_...``
    (D2) are both ``maks_jarak_sma20_1h_kali_atr`` here."""

    aktif: bool
    tf_pemicu: str
    ema_pemicu: int
    konfirmasi_dalam_candle: int
    maks_jarak_sma20_1h_kali_atr: float
    tp_persen: float
    sl_persen: float
    batas_waktu_menit: int


@dataclass(frozen=True)
class BreakoutConfig:
    """Mode B."""

    aktif: bool
    lookback_candle: int
    volume_kali_rata_rata: float
    periode_volume_rata_rata: int
    maks_jarak_di_atas_sma20_1h_kali_atr: float
    tp_persen: float
    sl_persen: float
    batas_waktu_menit: int


@dataclass(frozen=True)
class SidewaysConfig:
    """Mode C."""

    aktif: bool
    bollinger_periode: int
    bollinger_std: float
    min_jarak_tp_persen: float
    sl_persen: float
    batas_waktu_menit: int


@dataclass(frozen=True)
class NearMeanConfig:
    """Mode D, today's SHORT: SHORT when the price sits within ``maks_jarak_ke_ema_kali_atr`` ATR
    of the EMA, measured on the last ``candle_jendela`` closed ``tf_pemicu`` candles."""

    aktif: bool
    tf_pemicu: str
    candle_jendela: int
    ema_periode: int
    atr_periode: int
    maks_jarak_ke_ema_kali_atr: float
    tp_persen: float
    sl_persen: float
    batas_waktu_menit: int


@dataclass(frozen=True)
class TestConfig:
    """Block ``uji``, the test procedure of the document.

    Training is [latihan_mulai, latihan_selesai) and the test [latihan_selesai, ujian_selesai).
    The three dates are WIB midnights, aware datetimes in the +07:00 zone."""

    latihan_mulai: datetime
    latihan_selesai: datetime
    ujian_selesai: datetime
    lolos_hasil_per_1_juta_lebih_dari: float
    lolos_minimal_posisi: int
    lolos_buang_posisi_teratas: int
    lolos_hasil_tanpa_teratas_lebih_dari: float
    pindah_5m_jika_entry_per_hari_naik_kurang_dari: float
    hari_naik_minimal_jam_naik: int


@dataclass(frozen=True)
class ModesConfig:
    """The whole file: ``umum``, ``pemilih_mode``, ``pair``, the five ``mode.*`` blocks and ``uji``.

    ``pair`` is a read-only mapping keyed by pair name. ``dataclasses.asdict`` and
    ``copy.deepcopy`` cannot copy a read-only mapping, so read the fields instead."""

    umum: GeneralConfig
    pemilih_mode: SelectorConfig
    pair: Mapping[str, PairConfig]
    mode_a: PullbackConfig  # mode.A_pullback_long
    mode_b: BreakoutConfig  # mode.B_breakout_long
    mode_c: SidewaysConfig  # mode.C_sideways
    mode_d: NearMeanConfig  # mode.D_short_sekarang
    mode_d2: PullbackConfig  # mode.D2_pullback_short
    uji: TestConfig


_Reader = Callable[[Any, str], Any]


def _show(value: object) -> str:
    """``repr(value)`` that cannot raise. CPython will not print an int of more than 4300 digits,
    yet a mapping given to ``parse_modes_config`` may hold one, so a value named in a message
    must be safe."""
    try:
        return repr(value)
    except ValueError:
        return "<too many digits>"


def _join(path: str, key: object) -> str:
    name = key if isinstance(key, str) else _show(key)
    return f"{path}.{name}" if path else name


def _describe(value: object) -> str:
    return "null" if value is None else f"{_show(value)} ({type(value).__name__})"


def _fail(path: str, reason: str) -> ModesConfigError:
    return ModesConfigError(f"{path or 'top level'}: {reason}")


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise _fail(path, f"expected true or false, got {_describe(value)}")
    return value


def _must_be_false(value: Any, path: str) -> bool:
    if _boolean(value, path):
        raise _fail(path, "must be false: the AI may not open or close positions")
    return False


def _number(value: Any, path: str) -> float:
    """A finite int or float, returned as a float. A bool is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(path, f"expected a number, got {_describe(value)}")
    try:
        number = float(value)
    except OverflowError:  # an int beyond the float range
        raise _fail(path, "number is too large") from None
    if not math.isfinite(number):
        raise _fail(path, f"expected a finite number, got {value!r}")
    return number


def _positive(value: Any, path: str) -> float:
    number = _number(value, path)
    if number <= 0:
        raise _fail(path, f"must be greater than 0, got {value!r}")
    return number


def _integer(minimum: int) -> _Reader:
    def read(value: Any, path: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise _fail(path, f"expected a whole number, got {_describe(value)}")
        if value < minimum:
            raise _fail(path, f"must be at least {minimum}, got {_show(value)}")
        return value

    return read


def _one_of(*options: str) -> _Reader:
    wanted = " or ".join(repr(option) for option in options)

    def read(value: Any, path: str) -> str:
        if not isinstance(value, str) or value not in options:
            raise _fail(path, f"must be {wanted}, got {_describe(value)}")
        return value

    return read


def _faktor_k(value: Any, path: str) -> float | None:
    """A number of at least 1.0, or ``otomatis`` (returned as None)."""
    if isinstance(value, str):
        if value != _AUTOMATIC:
            raise _fail(path, f"a text value must be '{_AUTOMATIC}', got {value!r}")
        return None
    number = _number(value, path)
    if number < 1.0:
        raise _fail(path, f"must be at least 1.0 or '{_AUTOMATIC}', got {value!r}")
    return number


def _weekly_window(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise _fail(path, f"expected text such as 'mon 07:00-sat 00:00', got {_describe(value)}")
    try:
        window = parse_weekly_window(value)
    except ValueError as exc:
        raise _fail(path, str(exc)) from exc
    if window is None:
        raise _fail(path, "must not be empty: the weekly window gates every entry")
    return value


def _wib_midnight(value: Any, path: str) -> datetime:
    """A quoted ``YYYY-MM-DDTHH:MM:SS+HH:MM`` string at midnight in Asia/Jakarta, returned in WIB.

    The shape is pinned instead of left to ``fromisoformat``, which accepts more on 3.11 than
    on 3.10 (a trailing ``Z``, ``+0700``): one file must validate the same on both."""
    if not isinstance(value, str) or _INSTANT.fullmatch(value) is None:
        raise _fail(
            path,
            "expected a quoted string such as '2025-08-01T00:00:00+07:00' "
            f"(date, time and UTC offset), got {_describe(value)}",
        )
    try:
        local = datetime.fromisoformat(value).astimezone(_WIB)
    except (ValueError, OverflowError):  # month 13; or year 1, which has no UTC to convert via
        raise _fail(path, f"not a valid date and time in the supported range: {value!r}") from None
    if local.time() != time(0):
        raise _fail(
            path, f"must be midnight in {_WIB_ZONE} (the data is per WIB day), got {value!r}"
        )
    return local


_count = _integer(1)
_count_or_zero = _integer(0)

_GENERAL: dict[str, _Reader] = {
    "zona_waktu": _one_of(_WIB_ZONE),
    "fee_taker_persen": _positive,
    "slippage_persen": _positive,
    "maks_posisi_total": _count,
    "maks_posisi_per_pair": _count,
    "maks_entry_per_hari_total": _count,
    "maks_entry_per_hari_per_pair": _count,
    "jeda_menit": _count_or_zero,
    "penjaga_volatilitas_kali_median_atr": _positive,
    "ai_boleh_buka_tutup": _must_be_false,
    "notional_usdt": _positive,
    "jendela_entry_mingguan": _weekly_window,
}
_SELECTOR: dict[str, _Reader] = {
    "sma_cepat_1h": _count,
    "sma_lambat_1h": _count,
    "ema_4h": _count,
    "sideways_jarak_kali_atr": _positive,
}
_PAIR: dict[str, _Reader] = {
    "aktif": _boolean,
    "faktor_k": _faktor_k,
    "harus_sama_kondisi_dengan": _one_of(*_PAIRS),
}
_PULLBACK: dict[str, _Reader] = {
    "aktif": _boolean,
    "tf_pemicu": _one_of("15m", "5m"),
    "ema_pemicu": _count,
    "konfirmasi_dalam_candle": _count,
    "maks_jarak_sma20_1h_kali_atr": _positive,
    "tp_persen": _positive,
    "sl_persen": _positive,
    "batas_waktu_menit": _count,
}
_BREAKOUT: dict[str, _Reader] = {
    "aktif": _boolean,
    "lookback_candle": _count,
    "volume_kali_rata_rata": _positive,
    "periode_volume_rata_rata": _count,
    "maks_jarak_di_atas_sma20_1h_kali_atr": _positive,
    "tp_persen": _positive,
    "sl_persen": _positive,
    "batas_waktu_menit": _count,
}
_SIDEWAYS: dict[str, _Reader] = {
    "aktif": _boolean,
    "bollinger_periode": _count,
    "bollinger_std": _positive,
    "min_jarak_tp_persen": _positive,
    "sl_persen": _positive,
    "batas_waktu_menit": _count,
}
_NEAR_MEAN: dict[str, _Reader] = {
    "aktif": _boolean,
    "tf_pemicu": _one_of("3m"),
    "candle_jendela": _count,
    "ema_periode": _count,
    "atr_periode": _count,
    "maks_jarak_ke_ema_kali_atr": _positive,
    "tp_persen": _positive,
    "sl_persen": _positive,
    "batas_waktu_menit": _count,
}
_TEST: dict[str, _Reader] = {
    "latihan_mulai": _wib_midnight,
    "latihan_selesai": _wib_midnight,
    "ujian_selesai": _wib_midnight,
    "lolos_hasil_per_1_juta_lebih_dari": _number,
    "lolos_minimal_posisi": _count,
    "lolos_buang_posisi_teratas": _count_or_zero,
    "lolos_hasil_tanpa_teratas_lebih_dari": _number,
    "pindah_5m_jika_entry_per_hari_naik_kurang_dari": _positive,
    "hari_naik_minimal_jam_naik": _count,
}
_CAP_ABOVE = {"maks_jarak_sma20_1h_kali_atr": "maks_jarak_di_atas_sma20_1h_kali_atr"}
_CAP_BELOW = {"maks_jarak_sma20_1h_kali_atr": "maks_jarak_di_bawah_sma20_1h_kali_atr"}


def _check_keys(
    raw: Any, path: str, expected: Iterable[str], optional: Iterable[str] = ()
) -> None:
    """``raw`` must be a mapping holding exactly the expected keys (optional ones may be absent).

    Every unknown and missing key is named at once, each with its full path."""
    if not isinstance(raw, Mapping):
        raise _fail(path, f"expected a block of settings, got {_describe(raw)}")
    expected, optional = tuple(expected), tuple(optional)
    problems = [f"{_join(path, key)}: unknown key" for key in raw if key not in expected]
    problems += [
        f"{_join(path, key)}: missing key"
        for key in expected
        if key not in raw and key not in optional
    ]
    if problems:
        raise ModesConfigError("; ".join(problems))


def _read_block(
    raw: Any,
    path: str,
    cls: type,
    readers: Mapping[str, _Reader],
    *,
    renamed: Mapping[str, str] | None = None,
    optional: Iterable[str] = (),
) -> Any:
    """Check that ``raw`` holds exactly the keys of ``readers``, read each through its reader.

    ``readers`` maps a dataclass field to its reader, in YAML order. ``renamed`` maps the fields
    whose YAML key differs; ``optional`` names fields the file may omit (the default applies)."""
    keys = {field: (renamed or {}).get(field, field) for field in readers}
    _check_keys(raw, path, keys.values(), [keys[field] for field in optional])
    values = {
        field: read(raw[keys[field]], _join(path, keys[field]))
        for field, read in readers.items()
        if keys[field] in raw
    }
    return cls(**values)


def _read_pairs(raw: Any, path: str) -> Mapping[str, PairConfig]:
    _check_keys(raw, path, _PAIRS)
    return MappingProxyType(
        {
            name: _read_block(
                raw[name], _join(path, name), PairConfig, _PAIR,
                optional=("harus_sama_kondisi_dengan",),
            )
            for name in _PAIRS
        }
    )


def _read_test(raw: Any, path: str) -> TestConfig:
    test = _read_block(raw, path, TestConfig, _TEST)
    if not test.latihan_mulai < test.latihan_selesai < test.ujian_selesai:
        raise _fail(
            path,
            "latihan_mulai < latihan_selesai < ujian_selesai must hold: "
            "training ends on the day the test starts",
        )
    if not test.lolos_minimal_posisi > test.lolos_buang_posisi_teratas:
        raise _fail(
            path,
            "lolos_minimal_posisi > lolos_buang_posisi_teratas must hold: with no more positions "
            "than are trimmed, the pass rule has no result without the best ones to judge",
        )
    return test


def _read_mode(
    modes: Mapping[str, Any],
    key: str,
    cls: type,
    readers: Mapping[str, _Reader],
    renamed: Mapping[str, str] | None = None,
) -> Any:
    return _read_block(modes[key], _join("mode", key), cls, readers, renamed=renamed)


def parse_modes_config(doc: Mapping[str, Any]) -> ModesConfig:
    """Validate a mapping read from ``config/modes.yaml`` and return it as dataclasses."""
    _check_keys(doc, "", _TOP_LEVEL)
    modes = doc["mode"]
    _check_keys(modes, "mode", _MODES)
    return ModesConfig(
        umum=_read_block(doc["umum"], "umum", GeneralConfig, _GENERAL),
        pemilih_mode=_read_block(doc["pemilih_mode"], "pemilih_mode", SelectorConfig, _SELECTOR),
        pair=_read_pairs(doc["pair"], "pair"),
        mode_a=_read_mode(modes, "A_pullback_long", PullbackConfig, _PULLBACK, _CAP_ABOVE),
        mode_b=_read_mode(modes, "B_breakout_long", BreakoutConfig, _BREAKOUT),
        mode_c=_read_mode(modes, "C_sideways", SidewaysConfig, _SIDEWAYS),
        mode_d=_read_mode(modes, "D_short_sekarang", NearMeanConfig, _NEAR_MEAN),
        mode_d2=_read_mode(modes, "D2_pullback_short", PullbackConfig, _PULLBACK, _CAP_BELOW),
        uji=_read_test(doc["uji"], "uji"),
    )


_INT_TAG = "tag:yaml.org,2002:int"
_FLOAT_TAG = "tag:yaml.org,2002:float"
_MERGE_TAG = "tag:yaml.org,2002:merge"


class _PlainNumberLoader(yaml.SafeLoader):
    """``yaml.SafeLoader`` that reads a number only in plain decimals: ``15``, ``-150``, ``0.035``.

    Its YAML 1.1 rules also read ``015`` as octal 13, ``0x15``, ``0b1111``, ``1:30`` (base 60, so
    90) and ``25_000``. Here those stay text, so the readers refuse them as the wrong type."""

    yaml_implicit_resolvers = {
        first: [(tag, regexp) for tag, regexp in resolvers if tag not in (_INT_TAG, _FLOAT_TAG)]
        for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }


_DIGITS = list("-+0123456789")
_PlainNumberLoader.add_implicit_resolver(_INT_TAG, re.compile(r"^[-+]?(?:0|[1-9][0-9]*)$"), _DIGITS)
_PlainNumberLoader.add_implicit_resolver(
    _FLOAT_TAG, re.compile(r"^[-+]?(?:0|[1-9][0-9]*)\.[0-9]+$"), _DIGITS
)


def _refuse_repeated_keys(node: yaml.Node, path: str, visited: set[int]) -> None:
    """Refuse a key written twice in one mapping, which ``yaml.safe_load`` resolves silently
    by keeping the last, and a merge key (``<<``), which sets keys a second time out of sight:
    the file must say each thing once.

    An alias points at a node that is already checked, and may point back at its own ancestor,
    so a node is walked once."""
    if id(node) in visited:
        return
    visited.add(id(node))
    if isinstance(node, yaml.SequenceNode):
        for item in node.value:
            _refuse_repeated_keys(item, path, visited)
    elif isinstance(node, yaml.MappingNode):
        seen: set[str] = set()
        for key_node, value_node in node.value:
            child = path
            if isinstance(key_node, yaml.ScalarNode):
                child = _join(path, key_node.value)
                if key_node.tag == _MERGE_TAG:
                    raise _fail(child, "a merge key is not allowed: write each key once")
                if key_node.value in seen:
                    raise _fail(child, "key written twice")
                seen.add(key_node.value)
            _refuse_repeated_keys(value_node, child, visited)


def _unreadable(exc: Exception) -> ModesConfigError:
    return ModesConfigError(f"cannot read as YAML: {exc}")


def _parse_yaml(text: str) -> Any:
    """The Python values of ``text``.

    Whatever PyYAML raises leaves as a ``ModesConfigError``: a ``YAMLError`` for bad syntax, but
    also a bare ``ValueError`` for an unquoted date no calendar has (``2025-13-01``), a
    ``KeyError`` for ``!!bool maybe`` or a ``RecursionError`` for absurd nesting. Only the library
    calls sit inside a ``try``, so the repeated-key refusal below is not wrapped a second time.
    Both calls use ``_PlainNumberLoader``, a ``SafeLoader``: safe loading, as ``yaml.safe_load``."""
    try:
        root = yaml.compose(text, Loader=_PlainNumberLoader)  # builds nodes only, no Python objects
    except Exception as exc:
        raise _unreadable(exc) from exc
    if root is not None:
        _refuse_repeated_keys(root, "", set())
    try:
        return yaml.load(text, Loader=_PlainNumberLoader)
    except Exception as exc:
        raise _unreadable(exc) from exc


def load_modes_config(path: Path) -> ModesConfig:
    """Read, parse and validate ``path``.

    A file that cannot be read raises ``OSError``. Anything else wrong with it raises
    ``ModesConfigError`` starting with the file name: bytes that are not UTF-8, YAML that cannot
    be parsed or built into values (a date no calendar has, say), or a rule that is broken."""
    return load_modes_config_with_sha256(path)[0]


def load_modes_config_with_sha256(path: Path) -> tuple[ModesConfig, str]:
    """The configuration of ``path`` and the SHA-256 of the bytes it was parsed from.

    The file is read once: those bytes are hashed and those bytes are parsed, so the sha256 a
    lock binds always belongs to the numbers the run used, even if the file is edited meanwhile
    (``config_sha256`` and ``load_modes_config`` each read the file). The text is read as text
    mode reads it, line ends turned into ``\\n``. Raises what ``load_modes_config`` raises."""
    path = Path(path)
    data = path.read_bytes()
    try:
        text = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        return parse_modes_config(_parse_yaml(text)), hashlib.sha256(data).hexdigest()
    except UnicodeDecodeError as exc:
        raise ModesConfigError(f"{path}: not valid UTF-8 text: {exc}") from exc
    except ModesConfigError as exc:
        raise ModesConfigError(f"{path}: {exc}") from exc


def config_sha256(path: Path) -> str:
    """SHA-256 of the file's bytes, comments and spacing included: what the lock file binds."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
