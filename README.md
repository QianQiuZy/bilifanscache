# bilifanscache

面向 B 站直播粉丝牌的轻量缓存服务。

服务启动后会持续轮询 `rooms.json` 中配置的所有主播 UID，拉取粉丝牌分页数据并写入内存缓存；同时提供按房间、按主播以及按用户维度的查询接口。

## 特性

- **全量轮询**：持续遍历 `rooms.json` 内全部房间，不再按分钟级批次等待。
- **固定频率**：每个房间抓取完成后默认等待 3 秒继续下一个房间。
- **多维查询**：
  - `GET /fans?room_id=`：按房间查询缓存。
  - `GET /fans?uid=`：按主播 UID 查询缓存。
  - `GET /search?uid=`：查询某个粉丝 UID 在所有缓存中的粉丝牌等级分布。
- **结构化返回**：`/fans` 返回 `code/msg/uid/room_id/medal`，便于客户端统一处理。

## 快速开始

### 1. 准备环境变量

```bash
cp .env.example .env
```

按需修改 `.env`（至少填写 `SESSDATA`）。

### 2. 准备 rooms.json

`rooms.json` 格式如下：

```json
{
  "1820703922": {
    "uid": 1048135385,
    "name": "花礼Harei",
    "medal": "小礼猫"
  }
}
```

字段说明：
- 顶层 key：`room_id`（字符串形式，程序内会转为整数）。
- `uid`：主播 UID（用于请求 B 站粉丝牌接口）。
- `name`：主播名称（元数据，不参与接口计算）。
- `medal`：粉丝牌名称（`/search` 的展示 key）。

### 3. 启动服务

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

## 接口文档

详见 [`api.md`](./api.md)。

## 运行机制说明

- 启动时读取 `rooms.json`，构建 `room_id -> 主播信息` 与 `uid -> room_id` 映射。
- 后台任务持续循环：按房间抓取分页粉丝牌列表并更新 `fans_cache_by_room`。
- 任一房间缓存尚未拉取完成前，请求该房间会返回 `503`。

## 注意事项

- 需提供有效的 `SESSDATA`，否则会导致 B 站接口返回异常。
- 当前实现使用内存缓存；重启服务后缓存会重新拉取。
- `rooms.json` 若在运行中变更，需重启服务使配置重新加载。
