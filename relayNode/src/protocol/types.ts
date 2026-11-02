/**
 * 中转协议的数据类型定义（只有类型，没有逻辑）。
 *
 * 负责的功能：
 * - 定义 WS 上两种帧的形状：文本帧（JSON 控制帧）与二进制帧（JSON 头 + \n + 字节流）；
 * - 定义节点能力描述、任务请求/结果、OpenAI 兼容层的出入参。
 *
 * 注册参数：无（纯类型模块，import 即用）。
 *
 * 帧一览（文本，全部是 JSON）：
 *   hello      节点/客户端 → Hub  握手（带 token、角色、能力）
 *   hello.ok   Hub → 节点/客户端  握手通过（带心跳周期、服务器时间）
 *   ping/pong  双向              心跳（谁都可以发，收到就回）
 *   task       Hub → 节点         派发任务（含上传清单 uploads）
 *   ack        节点 → Hub         接单确认（accepted=false 会被重新路由）
 *   progress   节点 → Hub         进度（percent / note / delta 增量文本）
 *   result     节点 → Hub         任务结果（payload + 附件数量）
 *   error      双向              错误（有 taskId 时表示该任务出错）
 *   cancel     Hub → 节点         取消任务
 *   drain      Hub → 节点         让节点停止接新任务（滚动升级用）
 *   node.stats 节点 → Hub         节点自报负载（给运维看，不参与路由判定）
 *
 * 二进制帧格式：<JSON 头>\n<原始字节>
 *   头：{ taskId, dir: 'up' | 'down', aidx, seq, last, mimeType, size }
 *   dir='up'   客户端/Hub → 节点（ASR 上传音频）
 *   dir='down' 节点 → Hub（TTS 音频、图片等回传）
 */

/** 连接角色：工作节点（跑模型）或订阅者（收事件通知）。 */
export type relay_role = 'node' | 'client';

/** 节点注册的能力描述。priority 越大越优先；concurrency 是并发上限，默认 1。 */
export interface capability_spec {
  /** 能力名，例如 chat / tts / asr / image */
  name: string;
  /** 优先级，默认 0；越大越先被选中 */
  priority?: number;
  /** 并发上限，默认 1 */
  concurrency?: number;
  /** 附加信息（模型名、显存、版本等），只用于展示 */
  meta?: Record<string, unknown>;
}

/** 二进制帧的 JSON 头。 */
export interface binary_header {
  /** 所属任务 id */
  taskId: string;
  /** 方向：up = 上行（给节点），down = 下行（给 Hub） */
  dir: 'up' | 'down';
  /** 附件下标，一个任务可以有多个附件（多张图、多段音频） */
  aidx: number;
  /** 分片序号，从 0 开始，必须连续 */
  seq: number;
  /** 是否最后一片 */
  last: boolean;
  /** MIME 类型，例如 audio/mpeg */
  mimeType?: string;
  /** 附件用途标记，例如 audio / image / file */
  kind?: string;
  /** 整个附件的总字节数（可选，用于节点预分配与校验） */
  size?: number;
}

/** 文本帧的最小形状：type 必填，其余字段按类型各自约定。 */
export interface relay_frame {
  type: string;
  [key: string]: unknown;
}

/** 任务状态机。 */
export type task_status =
  | 'queued'    // 已创建，等节点
  | 'assigned'  // 已派给某节点，等 ack
  | 'running'   // 节点已接单，正在跑
  | 'done'      // 成功
  | 'failed'    // 失败（含超时、重试耗尽）
  | 'cancelled' // 被取消
  | 'expired';  // 结果过期被清理

/** 任务附件声明（上行音频等），Hub 按这个清单给节点发二进制帧。 */
export interface upload_spec {
  aidx: number;
  mimeType: string;
  kind?: string;
  /** 字节数，节点据此判断收齐没有 */
  size: number;
}

/** 结果附件（节点回传的音频/图片）。 */
export interface result_attachment {
  aidx: number;
  mimeType: string;
  kind?: string;
  bytes: number;
}

/** 提交任务的入参（HTTP POST /v1/tasks 的 body）。 */
export interface task_request {
  /** 需要的能力名，例如 tts */
  capability: string;
  /** 业务入参，原样交给节点的处理函数 */
  input?: Record<string, unknown>;
  /** 任务级元信息（调用方、会话 id、跟踪号），只做透传与日志 */
  meta?: Record<string, unknown>;
  /** 覆盖默认超时（毫秒） */
  timeoutMs?: number;
}

/** 任务对外的只读视图（HTTP 返回）。 */
export interface task_view {
  id: string;
  capability: string;
  status: task_status;
  attempts: number;
  /** 派单代数（每次重派自增，用来丢弃上一轮的迟到结果） */
  generation: number;
  nodeId: string | null;
  createdAt: number;
  updatedAt: number;
  deadlineAt: number;
  progress: number;
  note?: string;
  payload?: unknown;
  error?: string;
  attachments: result_attachment[];
  meta?: Record<string, unknown>;
}
