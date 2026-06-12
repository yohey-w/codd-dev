from app import Account, transfer


def test_transfer():
    a = Account("a")
    b = Account("b")
    a.deposit(100)
    transfer(a, b, 40)
    assert a.balance == 60
    assert b.balance == 40
