from bug_return_value import add, multiply


def test_add():
    assert add(2, 3) == 5, f"add(2,3) should be 5, got {add(2,3)}"
    assert add(0, 0) == 0, f"add(0,0) should be 0, got {add(0,0)}"
    assert add(-1, 1) == 0, f"add(-1,1) should be 0, got {add(-1,1)}"


def test_multiply():
    assert multiply(2, 3) == 6, f"multiply(2,3) should be 6, got {multiply(2,3)}"
    assert multiply(0, 5) == 0, f"multiply(0,5) should be 0, got {multiply(0,5)}"
    assert multiply(-1, 3) == -3, f"multiply(-1,3) should be -3, got {multiply(-1,3)}"


if __name__ == "__main__":
    test_add()
    test_multiply()
    print("All tests passed!")
