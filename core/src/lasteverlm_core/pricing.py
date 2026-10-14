"""Model prices and the cost of a call.

`MODEL_PRICES` is JSON, `{"<model>": {"in": <cents per million tokens>,
"out": <cents per million tokens>}}`. A deployment's values override the
defaults shipped here, model by model. A model without a price is a
configuration error, not a free call: `cost_cents` raises, and a service
checks its configured models with `require_prices` at startup so the
failure happens before the first request, not inside it.

One price shape serves every usage kind; what a unit is depends on the
kind, and only the `in` side is used where there is no output:

- `llm`, `embedding`: tokens, `in` and `out` per million tokens.
- `tts`: seconds of generated speech, rounded up per call, as the
  platform reports them. `tokens_in` holds the seconds, `tokens_out` is
  0; `in` is cents per million seconds.
- `stt`: seconds of audio, rounded up per call. `tokens_in` holds the
  seconds, `tokens_out` is 0; `in` is cents per million seconds (the
  platform prices the model per second, so 0.00028 USD per second is
  `"in": 28000`).
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .settings import ConfigError, env_optional


class ModelPrice(BaseModel):
    """Cents per million tokens, input and output. Wire names are `in` and `out`."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    cents_in: Decimal = Field(alias="in", ge=0)
    cents_out: Decimal = Field(alias="out", ge=0)


ModelPrices = Mapping[str, ModelPrice]

# The platform's model list and prices are only visible with an inference
# key; until they have been read there, nothing is pretended here and every
# deployment sets MODEL_PRICES for the models it uses (parent: docs/architecture.md,
# "Model prices"). Keys are the model names the inference API accepts.
DEFAULT_MODEL_PRICES: dict[str, ModelPrice] = {}

_MILLION = Decimal(1_000_000)


class UnpricedModelError(ConfigError):
    """A model call was about to be priced with no price configured for it."""

    def __init__(self, model: str) -> None:
        super().__init__(
            f"no price configured for model {model!r}; add it to MODEL_PRICES "
            '({"<model>": {"in": <cents per million tokens>, "out": ...}})'
        )
        self.model = model


def parse_model_prices(
    raw: str | None, defaults: ModelPrices = DEFAULT_MODEL_PRICES
) -> dict[str, ModelPrice]:
    """Merge the `MODEL_PRICES` JSON over the defaults. Empty or unset keeps the defaults."""
    prices = dict(defaults)
    if raw is None or not raw.strip():
        return prices
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"MODEL_PRICES is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ConfigError("MODEL_PRICES must be a JSON object keyed by model name")
    for model, entry in parsed.items():
        try:
            prices[str(model)] = ModelPrice.model_validate(entry)
        except ValidationError as exc:
            raise ConfigError(
                f"MODEL_PRICES[{model!r}] needs non-negative numeric 'in' and 'out': {exc}"
            ) from exc
    return prices


def load_model_prices(env: Mapping[str, str] | None = None) -> dict[str, ModelPrice]:
    """`MODEL_PRICES` from the environment merged over the shipped defaults."""
    return parse_model_prices(env_optional("MODEL_PRICES", env))


def require_prices(prices: ModelPrices, models: Iterable[str]) -> None:
    """Fail at startup when a configured model has no price."""
    for model in models:
        if model not in prices:
            raise UnpricedModelError(model)


def cost_cents(prices: ModelPrices, model: str, tokens_in: int, tokens_out: int) -> int:
    """`ceil((tokens_in * in + tokens_out * out) / 1e6)`, prices per million tokens.

    Exact decimal arithmetic, so a price like 0.1 cents does not produce a
    one-cent rounding surprise at a million tokens. Raises for an unpriced
    model; a usage row is never written at a silent zero tariff.
    """
    if tokens_in < 0 or tokens_out < 0:
        raise ValueError("token counts cannot be negative")
    price = prices.get(model)
    if price is None:
        raise UnpricedModelError(model)
    total = (tokens_in * price.cents_in + tokens_out * price.cents_out) / _MILLION
    return math.ceil(total)
