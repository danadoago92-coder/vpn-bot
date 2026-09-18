"""Round-robin payment cards with an immutable card snapshot for each order."""
import json
import re


def format_card_number(value):
    """Format a valid card number for display without changing stored data."""
    digits = str(value or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    digits = re.sub(r"[\s-]", "", digits)
    if re.fullmatch(r"[0-9]{16}", digits):
        return "-".join(digits[index:index + 4] for index in range(0, 16, 4))
    return str(value or "")


def normalize_cards(cards):
    if isinstance(cards, str):
        try:
            cards = json.loads(cards)
        except ValueError:
            raise ValueError("لیست کارت‌ها باید JSON معتبر باشد.") from None
    if not isinstance(cards, (list, tuple)) or len(cards) > 30:
        raise ValueError("بین صفر تا ۳۰ کارت قابل تعریف است.")
    result = []
    for card in cards:
        if not isinstance(card, dict):
            raise ValueError("هر کارت باید number و holder داشته باشد.")
        number = str(card.get("number", card.get("card_number", ""))).translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
        number = re.sub(r"[\s-]", "", number)
        holder = str(card.get("holder", card.get("name", card.get("card_holder", "")))).strip()
        if not re.fullmatch(r"[0-9]{16}", number) or not holder or len(holder) > 100:
            raise ValueError("برای هر کارت، شمارهٔ ۱۶ رقمی و نام صاحب کارت لازم است.")
        if number not in {x["number"] for x in result}:
            result.append({"number": number, "holder": holder})
    return result


async def configured_cards(ctx):
    values = await ctx.bot_data["store"].get_settings()
    settings = ctx.bot_data["settings"]
    if "cards" in values:
        cards = normalize_cards(values["cards"])
        if cards:
            return cards
    # A card edited in the bot takes precedence over installation defaults.
    if "card_number" in values or "card_holder" in values:
        number = values.get("card_number", settings.card_number)
        holder = values.get("card_holder", settings.card_holder)
        return normalize_cards([{"number": number, "holder": holder}]) if number and holder else []
    cards = getattr(settings, "cards", ())
    if cards:
        return normalize_cards(cards)
    return normalize_cards([{"number": settings.card_number, "holder": settings.card_holder}]) if settings.card_number and settings.card_holder else []


async def get_next_card(store, order_id, cards):
    return await store.assign_card(order_id, normalize_cards(cards))
