"use strict";
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
Object.defineProperty(exports, "__esModule", { value: true });
//# sourceMappingURL=types.js.map