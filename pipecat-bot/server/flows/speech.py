"""Equivalent conversational variants. Every amount/date comes from callers' facts."""

from language import Band

EMPTY = {
    'orders': (
        ('There are no orders on this account.', 'I checked this account and found no orders.'),
        ('आपके account पर कोई orders नहीं हैं।', 'मैंने आपका account check किया। यहाँ कोई orders नहीं हैं।')),
    'cancel': (
        ('There are no orders eligible for cancellation. Only unshipped orders can be cancelled.',),
        ('अभी कोई order cancellation के लिए eligible नहीं है। केवल unshipped orders cancel हो सकते हैं।',)),
    'reschedule': (
        ('There are no incoming shipments to reschedule.',),
        ('अभी कोई incoming shipment नहीं है जिसकी delivery reschedule कर सकूँ।',)),
    'return': (
        ('There are no delivered orders to check for a return.',),
        ('अभी कोई delivered order नहीं है जिसके लिए return options check कर सकूँ।',)),
    'dispute': (
        ('There are no delivered packages on this account to investigate.',),
        ('इस account पर कोई delivered package नहीं है जिसके लिए investigation खोल सकूँ।',)),
    'refunds': (
        ('There are no refunds on this account.', 'I found no refund records on this account.'),
        ('आपके account पर कोई refund record नहीं है।', 'मैंने check किया। इस account पर कोई refund record नहीं मिला।')),
}


def empty_speech(deps, kind):
    variants = EMPTY[kind][0 if deps.tracker.band == Band.MOSTLY_ENGLISH else 1]
    index = deps.speech_counts.get(kind, 0)
    deps.speech_counts[kind] = index + 1
    return variants[index % len(variants)]


def policy_refusal(code: str, fallback: str) -> str:
    """Stable caller-facing explanation for a deterministic policy denial."""
    messages = {
        'already_shipped': 'यह order shipped या dispatch हो चुका है, इसलिए cancellation available नहीं है। Delivery के बाद मैं return eligibility check कर सकता हूँ।',
        'not_delivered': 'यह order अभी deliver नहीं हुआ है, इसलिए यह request अभी available नहीं है।',
        'non_returnable': 'यह item return policy के तहत eligible नहीं है, इसलिए return request create नहीं हो सकती।',
        'window_closed': 'इस item की return window closed हो चुकी है, इसलिए return या exchange available नहीं है।',
        'out_of_stock': 'अभी requested replacement या exchange variant in stock नहीं है। मैं available refund options check कर सकता हूँ।',
        'already_replaced': 'इस item का replacement पहले ही हो चुका है, इसलिए दूसरा replacement available नहीं है।',
        'already_active': 'इस item के लिए पहले से एक open return request है।',
        'already_completed': 'यह delivery पहले ही पूरी हो चुकी है, इसलिए इसे reschedule नहीं किया जा सकता।',
        'dispute_window_closed': 'Missing-delivery dispute delivery notification के 3 दिनों के भीतर report करनी होती है।',
    }
    return messages.get(code, fallback)
