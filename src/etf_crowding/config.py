"""Load and validate the configured ETF research universe."""

from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

import yaml

DEFAULT_ETF_UNIVERSE_PACKAGE = "etf_crowding.resources"
DEFAULT_ETF_UNIVERSE_FILENAME = "etf_universe.yaml"


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""

    def construct_mapping(
        self, node: yaml.MappingNode, deep: bool = False
    ) -> dict[object, object]:
        """Construct a mapping while rejecting ambiguous duplicate keys."""

        self.flatten_mapping(node)
        mapping: dict[object, object] = {}

        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate_key = key in mapping
            except TypeError as error:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found an unhashable mapping key",
                    key_node.start_mark,
                ) from error

            if duplicate_key:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )

            mapping[key] = self.construct_object(value_node, deep=deep)

        return mapping


@dataclass(frozen=True, slots=True)
class ETFDefinition:
    """A validated ETF definition from the project configuration.

    Attributes:
        ticker: Exchange ticker symbol used to identify the ETF.
        name: Official or commonly used ETF name.
        category: Research-universe category assigned by the project.
    """

    ticker: str
    name: str
    category: str


def _required_text(
    entry: Mapping[object, object], field: str, entry_number: int
) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"ETF entry {entry_number} must contain a non-empty string '{field}'."
        )
    return value.strip()


def load_etf_universe(
    config_path: Path | None = None,
) -> tuple[ETFDefinition, ...]:
    """Load and validate ETF definitions from a YAML configuration file.

    Args:
        config_path: External YAML file to load. Defaults to the ETF universe
            packaged with the library.

    Returns:
        An immutable tuple of validated ETF definitions in configuration order.

    Raises:
        FileNotFoundError: If the configuration file does not exist.
        OSError: If the configuration file cannot be read.
        ValueError: If the YAML is invalid or its ETF definitions are malformed.
    """

    if config_path is None:
        config_source = files(DEFAULT_ETF_UNIVERSE_PACKAGE).joinpath(
            DEFAULT_ETF_UNIVERSE_FILENAME
        )
        source_description = (
            f"{DEFAULT_ETF_UNIVERSE_PACKAGE}/{DEFAULT_ETF_UNIVERSE_FILENAME}"
        )
    else:
        config_source = config_path
        source_description = str(config_path)

    try:
        with config_source.open("r", encoding="utf-8") as config_file:
            raw_config: object = yaml.load(config_file, Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as error:
        raise ValueError(
            f"Invalid YAML in ETF universe config '{source_description}': {error}"
        ) from error

    if not isinstance(raw_config, dict):
        raise ValueError("ETF universe config must be a top-level mapping.")

    raw_entries = raw_config.get("etfs")
    if not isinstance(raw_entries, list):
        raise ValueError("ETF universe config must contain an 'etfs' list.")
    if not raw_entries:
        raise ValueError("ETF universe config must contain at least one ETF entry.")

    definitions: list[ETFDefinition] = []
    seen_tickers: set[str] = set()

    for entry_number, raw_entry in enumerate(raw_entries, start=1):
        if not isinstance(raw_entry, dict):
            raise ValueError(f"ETF entry {entry_number} must be a mapping.")

        ticker = _required_text(raw_entry, "ticker", entry_number)
        name = _required_text(raw_entry, "name", entry_number)
        category = _required_text(raw_entry, "category", entry_number)

        normalized_ticker = ticker.casefold()
        if normalized_ticker in seen_tickers:
            raise ValueError(
                f"Duplicate ETF ticker '{ticker}' in '{source_description}'."
            )
        seen_tickers.add(normalized_ticker)

        definitions.append(ETFDefinition(ticker=ticker, name=name, category=category))

    return tuple(definitions)
