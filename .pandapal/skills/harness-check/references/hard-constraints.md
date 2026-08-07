# 硬约束实现参考

> 编码级的具体实现手段，供 harness-check skill 审查时参考。

---

## 字段冻结手段（Python）

### 方案 1：`__slots__` + `__setattr__` 拦截（推荐）

```python
class ImmutableObject:
    __slots__ = ('_field_a', '_field_b', '_frozen')

    def __init__(self, field_a, field_b):
        object.__setattr__(self, '_field_a', field_a)
        object.__setattr__(self, '_field_b', field_b)
        object.__setattr__(self, '_frozen', True)

    def __setattr__(self, name, value):
        raise PermissionError(f"Immutable: cannot modify '{name}'")

    def __delattr__(self, name):
        raise PermissionError("Immutable: cannot delete attributes")

    @property
    def field_a(self):
        return self._field_a
```

### 方案 2：`dataclass(frozen=True)`（简单场景）

```python
@dataclass(frozen=True)
class SimpleConfig:
    max_steps: int = 10
    timeout: float = 30.0
```

弱点：`object.__setattr__` 能绕过。适合内部配置，不适合安全关键对象。

---

## 不可变容器对照表

```
可变类型         → 不可变替代
list            → tuple
dict            → types.MappingProxyType(dict)
set             → frozenset
str / int / bool → 天然不可变
自定义对象       → __slots__ + __setattr__ 冻结
```

---

## 深拷贝防引用泄露

```python
class SafeObject:
    def __init__(self, items: list):
        # ❌ 错误：直接存引用
        # self._items = items

        # ✅ 正确：深拷贝 + 转不可变
        object.__setattr__(self, '_items', tuple(items))
```

---

## 主路径 vs Hook 的区别

```
主路径（不可跳过）：
  硬编码在执行流程中
  不注册也会执行
  删不掉、关不掉
  适合：权限检查、审计日志、步数限制

Hook（可选扩展）：
  需要显式注册才生效
  不注册就不执行
  适合：自定义业务逻辑、自定义日志格式
```

---

## 审计日志同步写

```python
class AuditLog:
    def write_sync(self, event: str, **kwargs):
        record = {
            "timestamp": datetime.utcnow().isoformat(),
            "event": event,
            **kwargs
        }
        self._backend.write(record)  # 同步
        self._backend.flush()        # 立即刷盘

    # ❌ 不提供：disable() / set_enabled(False) / async_write()
```

---

## 敏感操作硬名单

```python
# frozenset → 不可变，业务层只能加不能减
_MUST_CONFIRM: frozenset = frozenset({
    ("file", "delete"),
    ("code", "execute"),
    ("email", "send"),
    ("database", "delete"),
})
```

---

## HC 编号速查

| 编号 | 约束 | 所属层 | 手段 |
|------|------|--------|------|
| HC1 | 字段冻结 | 各层有状态对象 | `__slots__` + `__setattr__` |
| HC2 | 深度不可变 | 各层容器字段 | tuple / frozenset / MappingProxyType |
| HC3 | Guard 嵌入主路径 | Engine（agent_loop） | 硬编码，不是 hook |
| HC4 | 审计日志同步写 | 观测层 | write_sync + 不可关闭 |
| HC5 | 执行上限不可变 | Behavior | ExecutionLimits 冻结 + 无 reset |
| HC6 | 敏感操作二次确认 | Behavior | frozenset 硬名单 + 只加不减 |
