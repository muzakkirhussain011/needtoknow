from rampart.pii import Redactor, _iban_ok, _luhn_ok, redact


def test_email_phone_and_national_id_are_masked():
    r = redact("Contact khalid.harbi@najm-logistics.example or +966 55 123 4567, ID 1023456789.")
    assert "khalid.harbi" not in r.text
    assert "+966" not in r.text
    assert "1023456789" not in r.text
    assert r.counts == {"EMAIL": 1, "PHONE": 1, "NATIONAL_ID": 1}


def test_saudi_local_mobile_format():
    r = redact("Call me on 055 123 4567 tomorrow")
    assert r.counts["PHONE"] == 1
    assert "055 123 4567" not in r.text


def test_same_value_gets_same_placeholder_across_texts():
    red = Redactor()
    a = red.redact("Email a@b.example now")
    b = red.redact("Again: a@b.example, and c@d.example")
    assert "[EMAIL_1]" in a and "[EMAIL_1]" in b and "[EMAIL_2]" in b
    assert red.counts["EMAIL"] == 2


def test_restore_round_trip():
    red = Redactor()
    masked = red.redact("Send to a@b.example and 1023456789")
    assert red.restore(masked) == "Send to a@b.example and 1023456789"


def test_valid_card_masked_invalid_number_left_alone():
    assert _luhn_ok("4111 1111 1111 1111")
    assert not _luhn_ok("4111 1111 1111 1112")
    r = redact("card 4111 1111 1111 1111 and order 4111 1111 1111 1112")
    assert r.counts["CARD"] == 1
    assert "4111 1111 1111 1112" in r.text


def test_iban_needs_a_valid_checksum():
    assert _iban_ok("GB82 WEST 1234 5698 7654 32")
    assert not _iban_ok("GB00 WEST 1234 5698 7654 32")
    r = redact("Pay GB82 WEST 1234 5698 7654 32 today")
    assert r.counts["IBAN"] == 1


def test_secrets_are_masked():
    r = redact("key AKIAIOSFODNN7EXAMPLE and token ghp_" + "a" * 36)
    assert r.counts["SECRET"] == 2


def test_ordinary_numbers_are_not_masked():
    text = "Invoice 2026-0042 for SAR 50,000 is due in 30 days; 21 days of leave; order #998877."
    assert redact(text).text == text
