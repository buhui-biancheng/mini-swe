from bug_undefined_var import greet, farewell


def test_greet():
    assert greet("Alice") == "Hello, Alice!", f"greet('Alice') should be 'Hello, Alice!', got {greet('Alice')}"
    assert greet("Bob") == "Hello, Bob!", f"greet('Bob') should be 'Hello, Bob!', got {greet('Bob')}"


def test_farewell():
    assert farewell("Alice") == "Goodbye, Alice!", f"farewell('Alice') should be 'Goodbye, Alice!', got {farewell('Alice')}"
    assert farewell("Bob") == "Goodbye, Bob!", f"farewell('Bob') should be 'Goodbye, Bob!', got {farewell('Bob')}"


if __name__ == "__main__":
    test_greet()
    test_farewell()
    print("All tests passed!")
