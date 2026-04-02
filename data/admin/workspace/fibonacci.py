def fibonacci(n):
    a, b = 0, 1
    for _ in range(n - 1):
        a, b = b, a + b
    return a

if __name__ == "__main__":
    result = fibonacci(20)
    print(f"The 20th Fibonacci number is: {result}")
