"""Exercise find_tickets query building. No network or credentials needed."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from server import _build_ticket_query, _quote_if_needed, _phone_variants

cases = [
    ("email only",
     _build_ticket_query(email="ana@acme.com"),
     "type:ticket requester:ana@acme.com"),

    ("multi-word subject is quoted",
     _build_ticket_query(subject="cannot log in"),
     'type:ticket subject:"cannot log in"'),

    ("single-word subject is not quoted",
     _build_ticket_query(subject="invoice"),
     "type:ticket subject:invoice"),

    ("email and subject are combined",
     _build_ticket_query(email="ana@acme.com", subject="invoice"),
     "type:ticket requester:ana@acme.com subject:invoice"),

    ("single phone user becomes one requester",
     _build_ticket_query(requester_ids=[123]),
     "type:ticket requester:123"),

    ("several phone users become an OR group",
     _build_ticket_query(requester_ids=[1, 2]),
     "type:ticket (requester:1 requester:2)"),

    ("phone users combine with email and subject",
     _build_ticket_query(email="ana@acme.com", subject="cannot log in",
                         requester_ids=[1, 2]),
     'type:ticket requester:ana@acme.com subject:"cannot log in" '
     "(requester:1 requester:2)"),

    ("no filters leaves only the type",
     _build_ticket_query(),
     "type:ticket"),

    ("already-quoted values are not quoted again",
     _quote_if_needed('"already quoted"'),
     '"already quoted"'),

    ("surrounding whitespace is trimmed",
     _quote_if_needed("  invoice  "),
     "invoice"),

    ("phone without + is also tried with +",
     _phone_variants("573001234567"),
     ["573001234567", "+573001234567"]),

    ("phone with + is not duplicated",
     _phone_variants("+573001234567"),
     ["+573001234567"]),
]

failures = 0
for name, got, expected in cases:
    passed = got == expected
    failures += 0 if passed else 1
    print(("  PASS  " if passed else "  FAIL  ") + name)
    if not passed:
        print(f"          expected: {expected!r}")
        print(f"          got:      {got!r}")

print("\nALL_OK" if not failures else f"\n{failures} FAILURES")
sys.exit(1 if failures else 0)
