# flow-style：接口函数当编排函数

`SKILL.md` 的补充。只在把 FastAPI 的接口函数（`@router.get(...)`、`@app.post(...)` 标的函数）直接写成编排函数时
相关：接口本身就是一条功能线，函数体只写走向，干活的事都在步骤里。

## 怎么写

`@workflow` 和路由装饰器写在同一个函数上，`@workflow` 写在上面；出口用 `return` 响应和 `raise HTTPException`，
不用 `outcome`：

<!-- ok -->
```python
from fastapi import APIRouter, Depends, HTTPException
from pystepkit import step, workflow
from app.db import get_db

router = APIRouter(prefix="/users")

@step(title="查找用户")
def find_user(db, uid: int): ...

@step(title="停用并保存，返回用户信息")
def save_disabled(db, user) -> dict: ...

@workflow(title="停用用户", group="系统管理")
@router.post("/{uid}/disable")
def disable_user(uid: int, db=Depends(get_db)):
    user = find_user(db, uid)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if not user.enabled:
        raise HTTPException(status_code=409, detail="用户已经停用")
    return save_disabled(db, user)
```

- **装饰器的顺序**：pystepkit 的标记只给函数挂属性、原样返回，不影响 FastAPI 注册路由。两个装饰器谁在上面分析
  结果都一样，统一把 `@workflow` 写在上面，读代码时先看到"这是一条功能线"。
- **图上是什么样**：类型是工作流，标题是 `title`，旁边显示"方法 路径"（`POST /users/{uid}/disable`）；没写 `group`
  时和别的接口一样按文件名分组，没写 `title` 时标题就是"方法 路径"。
- **成功的出口 `return` 响应**：步骤的结果（`return save_disabled(db, user)`、`return user`）或响应模型的构造
  （`return UserOut(id=user.id, name=user.name)`）。组装响应要计算时放进步骤，由步骤返回——
  `UserOut.model_validate(user)`、`jsonable_encoder(user)` 写在接口函数里报 SM001。
- **失败的出口 `raise HTTPException(status_code=…, detail="…")`**：图上是失败的结局，标题是"状态码 说明"，如
  "404 用户不存在"。状态码写整数或 `status.HTTP_404_NOT_FOUND`，`detail` 写字符串（f-string 也行），图上才读得通；
  状态码是变量时结局标题退回成"抛出 HTTPException(…)"的源码。
- **不用 `outcome`**：`outcome(...)` 返回的是普通字典，FastAPI 会把它原样当响应体发出去，`kind`、`title` 就混进了
  接口的响应。`outcome` 留给不是接口的工作流（后台任务、定时任务、被派发的工作流）。
- **依赖写在参数里**：`db=Depends(get_db)`、当前用户等写在函数签名上，再作为参数传给步骤；不要在函数体里自己取
  会话、读请求（那是 SM001，或者是检查器查不出来但同样不允许的写法）。
- 别的规则照旧：函数体里只调用步骤和判断函数（SM001），条件里不做计算（SM002），不定义内部函数（SM004）。

## 检查

只有标了 `@workflow` 的接口函数才会被检查。没有标的接口照样出现在图里（类型是接口），但函数体不检查；
`--treat-as-marked fastapi` 不能让它们被检查（会报错）——要检查哪个接口，就给哪个接口加 `@workflow`。
