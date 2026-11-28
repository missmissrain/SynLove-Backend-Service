"use strict";
/**
 * 帧编解码：文本帧 ↔ 字符串、二进制帧 ↔ Buffer。
 *
 * 负责的功能：
 * - `encode_text` / `decode_text`：JSON 控制帧的序列化与容错解析（坏帧返回 null，绝不抛）；
 * - `encode_binary` / `decode_binary`：`<JSON 头>\n<字节>` 的拼装与拆解；
 * - `new_task_id` / `new_node_id` / `new_client_id`：带前缀的短 id；
 * - `to_buffer`：把 ws 回调里可能出现的 Buffer / ArrayBuffer / Buffer[] 统一成 Buffer。
 *
 * 为什么不用「长度前缀」而要 `\n` 分隔：JSON 头里不可能出现裸换行（会被转义成 \\n），
 * 所以第一个 0x0A 就是头与体的天然分界，解析只需要一次 indexOf，比读长度再切更省事。
 *
 * 注册参数：无（纯函数模块）。
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.new_task_id = new_task_id;
exports.new_node_id = new_node_id;
exports.new_client_id = new_client_id;
exports.encode_text = encode_text;
exports.decode_text = decode_text;
exports.encode_binary = encode_binary;
exports.decode_binary = decode_binary;
exports.to_buffer = to_buffer;
exports.slice_chunks = slice_chunks;
const node_crypto_1 = require("node:crypto");
/** 头部与字节流之间的分隔符。 */
const SEP = 0x0a;
/**
 * 生成任务 id。
 * 参数：无。返回形如 `tsk_1a2b3c4d5e6f7g8h` 的短 id（时间序 + 随机，便于日志排序）。
 */
function new_task_id() {
    return 'tsk_' + (0, node_crypto_1.randomBytes)(9).toString('hex');
}
/** 生成节点 id（Hub 在节点没自报 id 时使用）。 */
function new_node_id() {
    return 'nod_' + (0, node_crypto_1.randomBytes)(6).toString('hex');
}
/** 生成订阅者 id（客户端 WS 连接）。 */
function new_client_id() {
    return 'cli_' + (0, node_crypto_1.randomBytes)(6).toString('hex');
}
/**
 * 文本帧 → 字符串。
 * 参数 frame：任意控制帧（必须有 type 字段）。
 */
function encode_text(frame) {
    return JSON.stringify(frame);
}
/**
 * 字符串 → 文本帧对象。
 * 参数 raw：ws 收到的原始文本。坏 JSON 或非对象返回 null（调用方记日志后忽略即可）。
 */
function decode_text(raw) {
    if (!raw)
        return null;
    try {
        const data = JSON.parse(raw);
        if (!data || typeof data !== 'object')
            return null;
        if (typeof data.type !== 'string' || !data.type)
            return null;
        return data;
    }
    catch {
        return null;
    }
}
/**
 * 拼一个二进制帧。
 * 参数 head：JSON 头；body：这一片的原始字节。
 */
function encode_binary(head, body) {
    const header = Buffer.from(JSON.stringify(head), 'utf8');
    return Buffer.concat([header, Buffer.from([SEP]), body]);
}
/**
 * 拆一个二进制帧。
 * 参数 payload：ws 收到的二进制数据（Buffer/ArrayBuffer/Buffer[] 都可以）。
 * 返回 null 表示这不是合法二进制帧（没有分隔符、头不是合法 JSON、头缺字段）。
 */
function decode_binary(payload) {
    const buf = to_buffer(payload);
    if (!buf || buf.length === 0)
        return null;
    const cut = buf.indexOf(SEP);
    if (cut <= 0)
        return null;
    let head;
    try {
        head = JSON.parse(buf.subarray(0, cut).toString('utf8'));
    }
    catch {
        return null;
    }
    if (!head || typeof head !== 'object')
        return null;
    if (typeof head.taskId !== 'string' || !head.taskId)
        return null;
    if (head.dir !== 'up' && head.dir !== 'down')
        return null;
    return {
        header: {
            taskId: head.taskId,
            dir: head.dir,
            aidx: Number.isFinite(head.aidx) ? Number(head.aidx) : 0,
            seq: Number.isFinite(head.seq) ? Number(head.seq) : 0,
            last: Boolean(head.last),
            mimeType: typeof head.mimeType === 'string' ? head.mimeType : undefined,
            kind: typeof head.kind === 'string' ? head.kind : undefined,
            size: Number.isFinite(head.size) ? Number(head.size) : undefined,
        },
        body: buf.subarray(cut + 1),
    };
}
/**
 * 把 ws 回调里的各种二进制形态统一成 Buffer。
 * 参数 data：可能是 Buffer、ArrayBuffer、TypedArray、Buffer[]。
 */
function to_buffer(data) {
    if (Buffer.isBuffer(data))
        return data;
    if (Array.isArray(data))
        return Buffer.concat(data.map((item) => to_buffer(item)));
    if (data instanceof ArrayBuffer)
        return Buffer.from(data);
    if (ArrayBuffer.isView(data)) {
        const view = data;
        return Buffer.from(view.buffer, view.byteOffset, view.byteLength);
    }
    return Buffer.alloc(0);
}
/**
 * 把 Buffer 切成固定大小的片（用于上行/下行分片）。
 * 参数 buf：完整数据；size：每片字节数（默认 64KB，兼顾延迟与帧数）。
 */
function slice_chunks(buf, size = 64 * 1024) {
    if (buf.length <= size)
        return [buf];
    const out = [];
    for (let offset = 0; offset < buf.length; offset += size) {
        out.push(buf.subarray(offset, Math.min(offset + size, buf.length)));
    }
    return out;
}
//# sourceMappingURL=frames.js.map