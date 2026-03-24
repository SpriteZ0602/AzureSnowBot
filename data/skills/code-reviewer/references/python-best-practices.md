# Python 最佳实践速查

## 类型注解

```python
def greet(name: str, times: int = 1) -> str: ...
items: list[str] = []
mapping: dict[str, int] = {}
optional: str | None = None  # Python 3.10+
```

## 常见反模式

### 可变默认参数

```python
# ❌ 错误
def append(item, lst=[]):
    lst.append(item)
    return lst

# ✅ 正确
def append(item, lst=None):
    if lst is None:
        lst = []
    lst.append(item)
    return lst
```

### 裸 except

```python
# ❌ 错误
try:
    do_something()
except:
    pass

# ✅ 正确
try:
    do_something()
except (ValueError, TypeError) as e:
    logger.error(f"Failed: {e}")
```

### 字符串拼接

```python
# ❌ 循环拼接
result = ""
for item in items:
    result += str(item) + ","

# ✅ join
result = ",".join(str(item) for item in items)

# ✅ f-string
msg = f"Hello, {name}! You have {count} messages."
```

## 异步编程

```python
# 并发执行多个协程
results = await asyncio.gather(fetch_a(), fetch_b(), fetch_c())

# 超时控制
async with asyncio.timeout(10):
    data = await slow_operation()

# 避免在 async 中使用阻塞调用
result = await loop.run_in_executor(None, blocking_func, arg)
```

## 性能提示

- `dict` / `set` 查找是 O(1)，优先用于成员检测
- 列表推导比 `for` + `append` 快 ~30%
- `slots=True`（dataclass）减少内存占用
- 大文件逐行读取，避免 `.read()` 一次性加载
