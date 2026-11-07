# 多知识库隔离与知识库管理系统 — 技术实施计划

## 一、背景与目标

### 1.1 现状问题

当前系统所有视频都写入同一个 Chroma collection：`bilibili_videos`。

- `app/services/rag.py` 中 `RAGService` 默认 `collection_name="bilibili_videos"`（L25）。
- `app/routers/knowledge.py` 中 `get_rag_service()` 是全局单例（L28、L37）。
- `app/routers/chat.py` 问答时只按 `folder_ids` / `bvids` 过滤（L550 起），没有"知识库"这个概念。
- 前端只有收藏夹页 + 对话工作台（`frontend/app/page.tsx` L179-202），没有知识库管理入口。

结果：做饭视频和编程视频进入同一个向量空间、共用同一个 system prompt，问答必然串库，且无法单独管理。

### 1.2 目标

1. **物理隔离**：一个知识库 = 一个独立 Chroma collection，问答默认只检索当前知识库。
2. **多对多归属**：同一视频允许同时属于多个知识库，不用重复转写、不用重复 embedding。
3. **知识库管理**：列表、创建、重命名、删除、提示词、统计、清空。
4. **视频管理**：分页、搜索、状态筛选、单条删除、单条重建、批量删除、复制到其他知识库。
5. **对话切换**：对话页可切换知识库，并注入该库专属 system prompt。
6. **入库选库**：收藏夹入库/单视频入库时必须指定目标知识库。
7. **平滑迁移**：旧数据自动变成"默认知识库"，用户升级后无感。

### 1.3 已确认的设计决策（不再讨论）

| 决策 | 结论 |
| --- | --- |
| 隔离粒度 | 一库一 collection，命名 `kb_{id}` |
| 视频归属 | 多对多，联合唯一 `(kb_id, bvid)` |
| 跨库添加 | 复用源 collection 中已有 embeddings，不重新 embedding |
| 旧数据迁移 | 启动时若存在旧 collection 且知识库表为空，自动创建"默认知识库"并回填 |
| 库提示词 | `knowledge_bases.system_prompt`，问答时注入 system message |
| 删除语义 | 删除视频只删该库关联与该库向量，不删 `video_cache` |
| 转写缓存 | `video_cache` 继续全局共享，跨库复用 ASR 原文 |

---

## 二、总体架构

### 2.1 数据关系

```
KnowledgeBase (1) ──< KnowledgeBaseVideo >── (1) VideoCache
        │                     │
        │                     └── source_folder_id（可选，记录入库来源）
        └── collection_name: "bilibili_videos"（默认库）或 "kb_{id}"
```

- `VideoCache`：仍是一视频一条，存标题、简介、ASR 原文、处理状态。
- `KnowledgeBaseVideo`：视频在某个知识库中的归属记录。
- Chroma：每个知识库一个 collection，文档 metadata 维持现有结构（`bvid/title/source/doc_type/chunk_index/url`）。

### 2.2 请求链路

```
前端选择 kbId
  ├─ 入库：/knowledge/build | /folders/sync | /video/{bvid}/ingest  →  解析 kbId → RAGService(kb.collection_name)
  ├─ 管理：/knowledge/bases/*                                        →  解析 kbId → 该库 collection
  └─ 对话：/chat/ask|ask/stream { kb_id }                             →  该库 collection + 该库 bvids + system_prompt
```

### 2.3 兼容策略

- 旧 collection 名称 `bilibili_videos` **不改名**，直接作为默认知识库的 collection。
- 所有旧接口不传 `kb_id` 时，自动落到默认知识库，保持原调用方式可用。
- 旧测试 `test/test_chat_stream.py`、`test/test_failure_visibility.py` 依赖的 `get_rag_service` 符号保留，签名改为可选参数。

---

## 三、数据模型与迁移

### 3.1 新增表（app/models.py）

```python
class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    system_prompt = Column(Text, nullable=True)      # 该库专属人设
    collection_name = Column(String(64), unique=True, nullable=False)
    owner_mid = Column(Integer, index=True, nullable=True)  # B站用户 MID，兼容多账号
    is_default = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class KnowledgeBaseVideo(Base):
    __tablename__ = "knowledge_base_videos"
    __table_args__ = (
        UniqueConstraint("kb_id", "bvid", name="uq_kb_video"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    kb_id = Column(Integer, index=True, nullable=False)
    bvid = Column(String(20), index=True, nullable=False)
    source_folder_id = Column(Integer, nullable=True)  # FavoriteFolder.id，手动导入为 NULL
    created_at = Column(DateTime, default=datetime.utcnow)
```

说明：

- 不依赖 SQLite 外键级联（现有 `app/database.py` 未开启 `PRAGMA foreign_keys`），删除时全部显式处理。
- `owner_mid` 用于按 B 站账号隔离；默认为 `NULL` 的旧数据对登录用户可见。
- `source_folder_id` 用于收藏夹同步时判断"该视频是否因为离开收藏夹而需要从本库移除"，避免误删手动导入的视频。

### 3.2 ChatRequest 扩展（app/models.py L154）

```python
class ChatRequest(BaseModel):
    question: str
    session_id: Optional[str] = None
    folder_ids: Optional[list[int]] = None
    kb_id: Optional[int] = None   # 新增
```

### 3.3 迁移流程（新增 app/services/knowledge_base.py）

`ensure_default_knowledge_base()`，在 `app/main.py` 的 `lifespan` 中 `init_db()` 之后调用：

1. 查询 `knowledge_bases` 数量，非 0 直接返回（幂等）。
2. 创建 `KnowledgeBase(name="默认知识库", collection_name="bilibili_videos", is_default=True, owner_mid=None)`。
3. 读取旧 collection `bilibili_videos` 中所有 `bvid`（`collection.get(include=["metadatas"])`）。
4. 批量写入 `KnowledgeBaseVideo(kb_id=默认库.id, bvid=..., source_folder_id=None)`。
5. 任何一步失败只记录日志，不阻塞应用启动。

不创建 Alembic：本次只新增表，`Base.metadata.create_all` 足够；不修改既有表字段，无需 `ALTER TABLE`。

### 3.4 库与 collection 命名规则

| 场景 | collection_name |
| --- | --- |
| 迁移生成的默认库 | `bilibili_videos`（沿用旧集合） |
| 新建知识库 | 先插入记录拿到 id，再写 `kb_{id}` |
| 删除默认库后再新建 | 新库为 `kb_{id}`，并自动标记 `is_default=True` |

---

## 四、RAG 服务改造（app/services/rag.py）

### 4.1 实例注册表

把 `app/routers/knowledge.py` 的全局单例下沉到 `rag.py`：

```python
_services: dict[str, RAGService] = {}
_services_lock = threading.Lock()

def get_rag_service(collection_name: str = "bilibili_videos") -> RAGService:
    with _services_lock:
        service = _services.get(collection_name)
        if service is None:
            service = RAGService(collection_name=collection_name)
            _services[collection_name] = service
        return service

def drop_rag_service(collection_name: str) -> None:
    with _services_lock:
        _services.pop(collection_name, None)
```

- 避免每次请求重复初始化 `DashScopeEmbeddings` 和 Chroma client。
- `knowledge.py` 保留 `get_rag_service = ...` 的再导出，减少旧代码与测试改动。

### 4.2 新增方法

| 方法 | 用途 |
| --- | --- |
| `get_chunk_count() -> int` | 只做 `collection.count()`，供列表页统计，避免逐条扫 metadata |
| `get_all_bvids() -> list[str]` | 迁移回填；一次性扫描 metadata |
| `filter_present_bvids(bvids) -> set[str]` | 分批（100/批）用 `where={"bvid": {"$in": ...}}` 查询，供视频列表状态校验 |
| `copy_video_from(source: RAGService, bvid: str) -> int` | 跨库复用向量 |
| `delete_videos(bvids: list[str])` | 批量删除，较小视频逐条调用更简单，内部仍可分批 |
| `delete_collection()` | 删除整个知识库时使用（`vectorstore.delete_collection()`） |

`copy_video_from` 关键实现：

```python
data = source.vectorstore._collection.get(
    where={"bvid": bvid},
    include=["embeddings", "documents", "metadatas"],
)
if not data["ids"]:
    return 0
self.delete_video(bvid)  # 幂等，避免残留导致 id 冲突
self.vectorstore._collection.add(
    ids=data["ids"],
    embeddings=[e.tolist() if hasattr(e, "tolist") else e for e in data["embeddings"]],
    documents=data["documents"],
    metadatas=data["metadatas"],
)
return len(data["ids"])
```

注意：

- 不同 collection 之间可以复用相同 id，不存在冲突。
- `chromadb 0.5.15` 的 `get(include=["embeddings"])` 可能返回 numpy 数组，必须转 `list`。
- 复制完成后不修改 `video_cache`，ASR 原文继续复用。

### 4.3 统计口径调整

- `get_collection_stats()` 保持现有行为，但仅供单库详情/兼容接口使用。
- 知识库列表不调用它（会全量扫 metadata 且慢），改为：`video_count` 来自 SQL 关联表计数，`chunk_count` 来自 `get_chunk_count()`。

---

## 五、后端 API 设计

### 5.1 新增路由文件 app/routers/knowledge_bases.py

prefix：`/knowledge/bases`，在 `app/main.py` 注册。

所有写操作要求 `session_id`，通过 `UserSession.bili_mid` 解析 `owner_mid`；列表查询条件为 `owner_mid == mid OR owner_mid IS NULL`。

#### 知识库 CRUD

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/knowledge/bases?session_id=` | 列表 + 统计 |
| POST | `/knowledge/bases?session_id=` | 创建 |
| GET | `/knowledge/bases/{kb_id}?session_id=` | 详情 |
| PATCH | `/knowledge/bases/{kb_id}?session_id=` | 改名/简介/提示词 |
| DELETE | `/knowledge/bases/{kb_id}?session_id=` | 删除知识库 |
| POST | `/knowledge/bases/{kb_id}/clear?session_id=` | 清空本库向量与关联，保留库 |

创建请求：

```json
{ "name": "做饭", "description": "家常菜学习", "system_prompt": "你是一位家常菜厨师..." }
```

列表/详情响应：

```json
{
  "id": 2,
  "name": "做饭",
  "description": "家常菜学习",
  "system_prompt": "你是一位家常菜厨师...",
  "collection_name": "kb_2",
  "is_default": false,
  "video_count": 18,
  "chunk_count": 326,
  "failed_count": 1,
  "pending_count": 0,
  "created_at": "2026-11-07T10:00:00",
  "updated_at": "2026-11-07T10:30:00"
}
```

校验规则：

- `name` 去空格后长度 1-50，同 `owner_mid` 下不允许重名 → 409。
- `system_prompt` 最长 4000 字。
- 删除最后一个库 → 400「至少保留一个知识库」。
- 该库有运行中的构建任务 → 409「该知识库正在入库，请完成后再操作」。
- 删除默认库后，若有其他库，把最早创建的库设为默认；若没有，删除被拒绝（同上一条）。

删除执行顺序：

1. 删除 `KnowledgeBaseVideo` 关联。
2. `drop_rag_service(collection_name)`。
3. 删除 Chroma collection。
4. 删除 `KnowledgeBase` 记录。
5. **不删除** `video_cache`。

#### 知识库视频管理

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/knowledge/bases/{kb_id}/videos` | 分页列表 |
| DELETE | `/knowledge/bases/{kb_id}/videos/{bvid}` | 从本库移除 |
| POST | `/knowledge/bases/{kb_id}/videos/batch-delete` | 批量移除 |
| POST | `/knowledge/bases/{kb_id}/videos/{bvid}/rebuild` | 用缓存原文重建向量 |
| POST | `/knowledge/bases/{kb_id}/videos/import` | 从其他库复制视频 |

列表参数：

- `page`（默认 1）
- `page_size`（默认 20，最大 100）
- `q`（匹配 `bvid/title/owner_name`）
- `status`：`all | ready | failed | pending`（默认 all）
- 排序：`knowledge_base_videos.created_at DESC`

列表项：

```json
{
  "bvid": "BV1xx",
  "title": "视频标题",
  "owner_name": "UP主",
  "duration": 620,
  "pic_url": "https://...",
  "content_source": "asr",
  "status": "ready",
  "process_error": null,
  "chunk_count": 14,
  "added_at": "2026-11-07T10:00:00",
  "url": "https://www.bilibili.com/video/BV1xx"
}
```

状态判定（顺序）：

1. `video_cache` 不存在 → `missing`
2. `process_error` 非空 → `failed`
3. `is_processed == false` → `pending`
4. 本库向量不存在 → `missing`
5. 其他 → `ready`

`has_vectors` 只对当前页的 bvid 做一次 `filter_present_bvids`，避免 N 次查询。`status=missing` 不提供筛选（需要全量校验，成本高），只在行内展示。

移除视频：

- 删除本库 `KnowledgeBaseVideo` 记录。
- 删除本库 collection 中该 bvid 的向量。
- 不删除 `video_cache`，不影响其他知识库。
- 若该库是旧默认库且视频仍被其他库引用，其他库有独立副本，互不影响。

重建视频：

- 读取 `video_cache.content`。
- 无内容或长度 < 10 → 409「缺少原文，请在收藏夹中重新入库」。
- 构造 `VideoContent`（`source` 从 `cache.content_source` 映射，非法值回退 `basic_info`；`outline=cache.outline_json`）。
- 删除本库旧向量 → `add_video_content`。
- 不触发 ASR，不产生转写费用，只产生 embedding 费用。
- v1 不接取消操作（单视频重建通常较快）。

从其他库导入：

```json
{ "bvids": ["BV1", "BV2"], "source_kb_id": 3 }
```

逻辑：

1. 已在本库 → `already`。
2. 指定 `source_kb_id` 且该库有向量 → 复制。
3. 未指定 → 在该用户所有知识库中查找第一个有该视频向量的库 → 复制。
4. 找不到 → `not_found`（不会自动触发 ASR，避免误产生费用）。
5. 复制成功 → 写 `KnowledgeBaseVideo` 关联（`source_folder_id=None`）。

响应：

```json
{
  "copied": 1,
  "already": 1,
  "not_found": ["BV3"],
  "results": [
    { "bvid": "BV1", "status": "copied", "chunks": 12 },
    { "bvid": "BV2", "status": "already" }
  ]
}
```

### 5.2 改造现有 app/routers/knowledge.py

| 接口 | 改动 |
| --- | --- |
| `GET /knowledge/stats` | 增加可选 `kb_id`，默认库；返回该库 `total_chunks/total_videos`（视频数改为 SQL 计数） |
| `GET /knowledge/folders/status` | 增加可选 `kb_id`；`indexed_count` 改为"本库中属于该收藏夹且有向量的视频数" |
| `POST /knowledge/folders/sync` | `SyncRequest` 增加 `kb_id`；同步写入 `KnowledgeBaseVideo` |
| `POST /knowledge/build` | `BuildRequest` 增加 `kb_id`；`BuildStatus` 增加 `kb_id/kb_name` |
| `DELETE /knowledge/clear` | 增加可选 `kb_id`；清空该库 collection + 关联表 |
| `DELETE /knowledge/video/{bvid}` | 增加可选 `kb_id`；删本库关联与向量 |
| `POST /knowledge/video/{bvid}/ingest` | `SingleVideoIngestRequest` 增加 `kb_id` |
| `POST /knowledge/video/{bvid}/export` | 不改；导出与库无关 |

`get_rag_service()` 调整为按 collection 取实例：

```python
def _get_kb_rag(kb: KnowledgeBase) -> RAGService:
    return get_rag_service(kb.collection_name)
```

#### `_sync_folder` 改造要点

签名新增 `kb_id`，流程改动：

1. 收藏夹视频元数据写库后，对每个目标视频确保 `KnowledgeBaseVideo` 存在。
2. 目标库无向量时，先尝试**跨库复用**：
   - 查该用户其他库是否有该 bvid 向量；
   - 有则 `copy_video_from`，跳过 ASR 与 embedding；
   - 没有才走原 `fetch_content → add_video_content` 流程。
3. 向量入本库成功后，写关联；失败也写关联（便于在管理页看到失败项并重建），并记录 `process_error`。
4. 收藏夹移除视频时：
   - 仅处理 `source_folder_id == 当前 folder.id` 的关联；
   - 删除关联前检查该 bvid 是否仍被其他收藏夹引用（`FavoriteVideo` 计数）；
   - 仍被引用 → 保留关联与向量；
   - 无引用 → 删本库关联 + 删本库向量；
   - 其他知识库是独立 collection，不受影响；
   - `FavoriteVideo` 的清理逻辑保持不变。
5. `has_video` 判断改为本库 collection。

#### `_ingest_single_video` 改造要点

1. 解析目标 `kb_id`，`rag` 使用对应 collection。
2. 写缓存元数据后先写入 `KnowledgeBaseVideo`（取消操作时回滚删除，成功/失败保留，便于重试）。
3. 本库无向量时优先跨库复制；否则走原有 ASR + embedding。
4. `cache.is_processed` 继续表示"至少成功生成过一次向量"，不再作为单库状态依据。

### 5.3 对话改造（app/routers/chat.py）

#### 作用域对象

```python
@dataclass
class ChatScope:
    kb: Optional[KnowledgeBase]
    kb_id: Optional[int]
    kb_name: str
    collection_name: str      # 旧数据无 KB 时回退 "bilibili_videos"
    folder_ids: list[int]
    bvids: list[str]          # 本库视频；旧模式下由 folder_ids 推导
    restrict_whole_kb: bool   # 无 folder_ids 时为 True
```

解析逻辑：

1. `request.kb_id` 有值 → 取该库。
2. 无 `kb_id` → 取默认库。
3. 知识库表为空（首次启动未迁移成的极端情况）→ `kb=None`，回退旧行为：collection=`bilibili_videos`，bvids 由 `folder_ids` 推导。
4. `request.folder_ids` 有值 → `bvids` 与收藏夹视频取交集。
5. 无 `folder_ids` → 使用本库全部 bvid。

#### 检索范围优化

- `folder_ids` 为空时，collection 本身已是本库范围，向量检索 **不传 `bvids` filter**，减少 Chroma 过滤开销。
- `folder_ids` 非空时才传 `{"bvid": {"$in": 交集}}`。

#### 需要改写的辅助函数

| 函数 | 改动 |
| --- | --- |
| `_get_folder_ids_for_session` | 保留 |
| `_get_bvids_by_folder_ids` | 保留，用于收藏夹交集 |
| `_get_video_context` | 改为按 `scope.bvids` 查 `VideoCache`；有 folder 筛选时按收藏夹分组，否则按知识库名分组 |
| `_get_video_titles_context` | 同上，轻量查询 |
| `_keyword_search_docs` | `where(VideoCache.bvid.in_(scope.bvids))`，不再 join `FavoriteVideo` |
| `_is_related_to_collection` | 同上，按 `scope.bvids` 做 LIKE 计数 |
| `_prepare_messages` | 创建 `scope`，`rag = get_rag_service(scope.collection_name)` |

#### system prompt 注入

新增：

```python
def _apply_kb_prompt(messages: list[dict], kb: Optional[KnowledgeBase]) -> list[dict]:
    prompt = (kb.system_prompt or "").strip() if kb else ""
    if prompt and messages and messages[0]["role"] == "system":
        messages[0]["content"] = f"{prompt}\n\n{messages[0]['content']}"
    return messages
```

在 `_prepare_messages` 所有 return 出口统一套一层（用一个内部 `finish(messages, sources)` 闭包），覆盖 direct / db_list / db_content / vector / fallback 所有路由。

#### 其他接口

- `POST /chat/ask`、`/chat/ask/stream`：直接使用扩展后的 `ChatRequest`。
- `POST /chat/search`：增加可选 `kb_id`，用于该库内搜索。
- 前端流式请求体增加 `kb_id`。

---

## 六、前端设计

### 6.1 路由与页面

| 路径 | 内容 |
| --- | --- |
| `/` | 现有工作台，顶部增加知识库切换器 + "知识库管理"入口 |
| `/knowledge` | 知识库列表页 |
| `/knowledge/[kbId]` | 知识库详情（视频管理） |

登录态：从 `localStorage.bili_session` 读取；未登录显示"请先扫码登录"并给返回首页按钮。不引入新的全局状态库。

### 6.2 工作台改造

`frontend/app/page.tsx`：

- 新增 `kbs`、`activeKbId` 状态，登录后调用 `kbApi.list(sessionId)`。
- `activeKbId` 持久化到 `localStorage.active_kb_id`；失效时回落到默认库。
- 顶栏增加知识库下拉选择器与「知识库管理」链接。
- 把 `kbId` 传给 `SourcesPanel` 与 `ChatPanel`。
- 切换知识库时 `ChatPanel` 清空当前对话（避免历史回答属于旧库造成误解）。

`frontend/components/SourcesPanel.tsx`：

- Props 增加 `kbId`、`kbName`。
- `getFolderStatus`/`syncFolders`/`build`/`ingestVideo` 全部带上 `kb_id`。
- 入库确认弹窗显示"入库到：{kbName}"；没有可用知识库时禁用入库并提示先创建。
- 构建进度继续轮询 `/knowledge/build/status/{task_id}`。

`frontend/components/ChatPanel.tsx`：

- Props 增加 `kbId`、`kbName`。
- 流式请求体加入 `kb_id`；普通兜底请求也加入。
- 统计改为 `knowledgeApi.getStats(kbId)`；`kbId` 或 `statsKey` 变化时重新拉取。
- 标题显示「对话工作台 · {kbName}」。

### 6.3 知识库列表页 `/knowledge`

- 卡片网格，每张卡：名称、默认标记、简介、视频数、片段数、失败数、更新时间。
- 操作：进入、重命名、设置提示词、清空、删除。
- 新建弹窗：名称（必填）、简介、专属提示词。
- 删除/清空二次确认，文案说明"只影响本知识库，不删除视频原始缓存"。
- 空状态引导创建，并说明默认库已自动创建。

### 6.4 知识库详情页 `/knowledge/[kbId]`

- 顶部：返回、库名、统计、清空、删除、去工作台入库。
- 工具栏：搜索框（300ms 防抖）、状态筛选、每页条数。
- 表格列：勾选、标题（外链）、UP主、时长、状态、入库时间、操作（重建/删除/导出）。
- 批量操作：批量移除、复制到其他知识库（选择目标库）。
- 分页：上一页/下一页 + 总数。
- 状态标签：`ready=已就绪`、`failed=失败`、`pending=待处理`、`missing=向量缺失`。
- 所有写操作后刷新列表和顶部统计。

### 6.5 API 客户端（frontend/lib/api.ts）

新增类型：

- `KnowledgeBase`
- `KnowledgeBaseVideo`
- `KnowledgeBaseVideoList`
- `KnowledgeBaseImportResult`

新增 `kbApi`：

```ts
list(sessionId)
create(data, sessionId)
get(kbId, sessionId)
update(kbId, data, sessionId)
remove(kbId, sessionId)
clear(kbId, sessionId)
listVideos(kbId, params, sessionId)
removeVideo(kbId, bvid, sessionId)
batchRemoveVideos(kbId, bvids, sessionId)
rebuildVideo(kbId, bvid)
importVideos(kbId, data, sessionId)
```

现有 `knowledgeApi` 方法增加可选 `kbId` 参数（build/sync/getFolderStatus/getStats/clear/deleteVideo/ingestVideo），`chatApi.ask` 增加 `kbId`。

### 6.6 样式

在 `frontend/app/globals.css` 追加，复用现有 CSS 变量与 `.btn/.panel/.status-pill/.modal-*`：

- `.kb-grid`、`.kb-card`、`.kb-card-head`、`.kb-stats`
- `.kb-toolbar`、`.kb-table`、`.kb-table-row`、`.kb-checkbox`
- `.kb-pagination`
- `.kb-select`（顶栏选择器）
- `.modal-textarea`

---

## 七、文件变更清单

### 后端

| 文件 | 类型 | 内容 |
| --- | --- | --- |
| `app/models.py` | 修改 | 新增两张表；`ChatRequest.kb_id` |
| `app/services/rag.py` | 修改 | 实例注册表；count/bvids/copy/delete collection |
| `app/services/knowledge_base.py` | 新增 | KB 查询辅助、默认库迁移、关系读写、跨库查找 |
| `app/routers/knowledge_bases.py` | 新增 | KB CRUD、视频列表/删除/重建/导入 |
| `app/routers/knowledge.py` | 修改 | 入库/同步/统计/清空/删除全部按 kb 作用域 |
| `app/routers/chat.py` | 修改 | `ChatScope`、检索范围、system prompt 注入 |
| `app/main.py` | 修改 | 注册新路由；启动时执行默认库迁移 |
| `test/test_knowledge_bases.py` | 新增 | 迁移、CRUD、作用域、导入、删除单测 |
| `test/test_chat_stream.py` | 视情况修改 | 适配签名（保持兼容优先） |
| `test/test_failure_visibility.py` | 视情况修改 | 适配签名（保持兼容优先） |

### 前端

| 文件 | 类型 | 内容 |
| --- | --- | --- |
| `frontend/lib/api.ts` | 修改 | 新类型、`kbApi`、旧方法加 `kbId` |
| `frontend/app/page.tsx` | 修改 | KB 列表加载、顶栏切换器、管理入口、传参 |
| `frontend/components/SourcesPanel.tsx` | 修改 | 入库目标库、请求带 kb_id、确认弹窗展示 |
| `frontend/components/ChatPanel.tsx` | 修改 | 对话切库、请求带 kb_id、库内统计 |
| `frontend/components/KnowledgeBaseSelect.tsx` | 新增 | 顶栏知识库下拉组件 |
| `frontend/app/knowledge/page.tsx` | 新增 | 知识库列表与管理 |
| `frontend/app/knowledge/[kbId]/page.tsx` | 新增 | 视频管理 |
| `frontend/app/globals.css` | 修改 | 管理页样式 |
| `frontend/test/api.test.mjs` | 视情况修改 | 补充 kbApi 请求参数断言 |

---

## 八、实施步骤（依赖顺序）

### P1 后端基础与隔离

1. `app/models.py`：新增表与 `ChatRequest.kb_id`。
2. `app/services/rag.py`：实例注册表 + 新增方法。
3. `app/services/knowledge_base.py`：解析库、迁移、关联读写、跨库查找。
4. `app/main.py`：启动迁移 + 注册新路由（路由可先占位）。
5. `app/routers/knowledge.py`：入库、同步、统计、清空、删除全部改为按库。
6. `app/routers/chat.py`：`ChatScope` + 检索范围 + system prompt。
7. `app/routers/knowledge_bases.py`：CRUD + 视频管理接口。
8. 后端冒烟：创建两个库、分别入库、对话互不串库。

### P2 前端 API 与工作台

9. `frontend/lib/api.ts` 类型与请求改造。
10. `page.tsx` 顶栏切换器与管理入口。
11. `SourcesPanel` 入库目标库。
12. `ChatPanel` 对话切库与统计。

### P3 管理页面

13. `/knowledge` 列表页（CRUD + 提示词）。
14. `/knowledge/[kbId]` 详情页（分页/搜索/筛选/删除/重建/批量/复制）。
15. `globals.css` 样式补齐。

### P4 验证与兼容

16. 更新/新增后端测试。
17. 前端 lint + build。
18. 手工 E2E 与迁移验证。

---

## 九、验证清单

### 后端

- `python -m compileall app` 无语法错误。
- `python -m pytest test -q`（若环境有 pytest）或逐个运行现有脚本。
- 新增 `test/test_knowledge_bases.py` 覆盖：
  - 空库启动自动创建"默认知识库"且幂等；
  - 旧 collection 的 bvid 能回填到关联表；
  - 创建/重命名/删除知识库；
  - 同一视频可加入两个库；
  - 从 A 库导入到 B 库时 `copy_video_from` 被调用且不触发 ASR；
  - 删除单库只清理本库关联和向量；
  - `kb_id` 缺省时回退默认库。
- 启动应用后 `GET /knowledge/bases` 正常返回。
- 两个库分别入库同类视频，`POST /chat/ask` 命中来源不交叉。

### 前端

- `npm --prefix frontend run lint`
- `npm --prefix frontend run build`
- `node frontend/test/api.test.mjs`
- 手工流程：
  1. 登录 → 自动看到"默认知识库"。
  2. 新建"做饭"和"编程"。
  3. 工作台切到"做饭"，入库一个收藏夹；再切到"编程"，入库同一收藏夹。
  4. 第二次入库明显更快（跨库复制向量，无 ASR）。
  5. 分别在两个库提问，来源与回答互不串。
  6. 详情页搜索、筛选、删除、重建、批量删除、复制到其他库。
  7. 删除一个知识库，另一个库数据完好。
  8. 旧数据升级后默认库视频数量与升级前一致。

### 迁移与兼容

- 备份 `data/chroma_db` 与 `data/bilibili_rag.db` 后再首次启动。
- 启动日志出现"默认知识库已创建/回填 N 个视频"。
- 不传 `kb_id` 调用旧接口行为保持不变。

---

## 十、风险与处理

| 风险 | 处理 |
| --- | --- |
| 每个库一个 Chroma client，实例过多 | 按 collection 名缓存；删除库时清除缓存 |
| 复制向量时 numpy 数组类型不兼容 | 统一 `tolist()` 后写入 |
| `video_cache.is_processed` 是全局状态，不能表达单库状态 | 单库状态 = 关联表 + 本库向量存在性；UI 与接口均按此口径 |
| 收藏夹同步误删手动导入的视频 | `source_folder_id` 为空的手动导入不参与同步移除 |
| 两个库同时入库 | 沿用"每用户仅允许一个构建任务"；删除库前检查运行中任务 |
| 新表创建但旧库无数据 | 仍创建默认库，保证前端有默认选项 |
| 测试依赖 `get_rag_service` 无参调用 | 保留同名符号，参数可选；不改变旧调用行为 |
| 大库全量 `bvids` 过滤拖慢检索 | 整库问答不传 bvid filter，仅在按收藏夹筛选时传 |
| 删除 collection 不可恢复 | 前端二次确认；文案明确只影响该库 |

---

## 十一、成本控制说明

1. **跨库添加视频**：只复制 embeddings，不调用 ASR、不调用 embedding 接口。
2. **同一库重复入库**：`video_cache` 命中时不再转写，仅在内容缺失或来源升级时重新 embedding。
3. **单条重建**：只使用缓存原文重新 embedding，不重新转写。
4. **长视频**：首次入库仍由用户承担 ASR 与 embedding 费用，行为与当前一致；本方案不新增隐性调用。
5. **问答**：整库检索不附加 bvid 过滤，减少 Chroma 过滤与候选裁剪开销；模型调用次数不变。
