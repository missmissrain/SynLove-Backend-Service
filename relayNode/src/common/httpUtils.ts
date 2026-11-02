/**
 * HTTP 小工具：读 body、回 JSON/字节、SSE、鉴权、URL 解析。
 *
 * 负责的功能：
 * - `parse_url`：把 req.url 拆成 pathname + query（不做路由，路由在 hubServer）；
 * - `read_body` / `read_json`：带上限的 body 读取，超限直接拒绝（防止大文件把内存打爆）；
 * - `send_json` / `send_bytes` / `send_error`：统一响应格式（错误统一 `{error:{code,message}}`）；
 * - `send_sse_headers` / `send_sse` / `send_sse_done`：OpenAI 流式兼容层用的 SSE 写法；
 * - `token_ok`：Bearer 头或 `?token=` 二选一，任一匹配即通过（浏览器 EventSource 不能用自定义头）。
 *
 * 注册参数：无（纯函数模块）。
 */

import type { IncomingMessage, ServerResponse } from 'node:http';

/** 解析后的 URL。 */
export interface parsed_url {
  pathname: string;
  query: URLSearchParams;
}

/**
 * 解析请求 URL。
 * 参数 req：Node 的 IncomingMessage。
 */
export function parse_url(req: IncomingMessage): parsed_url {
  const url = new URL(req.url || '/', 'http://placeholder');
  return { pathname: url.pathname, query: url.searchParams };
}

/**
 * 读取请求体（带上限）。
 * 参数 req：请求；limit：最大字节数，默认 32MB。
 * 超限会 reject('body_too_large')，但**不销毁连接**：把剩下的字节丢掉继续读，
 * 这样调用方还能收到一个干净的 413。直接 destroy 的话客户端只会看到
 * 「连接被重置」，分不清是包太大还是网断了。
 */
export async function read_body(req: IncomingMessage, limit = 32 * 1024 * 1024): Promise<Buffer> {
  return new Promise<Buffer>((resolve, reject) => {
    const chunks: Buffer[] = [];
    let size = 0;
    let failed = false;
    req.on('data', (chunk: Buffer) => {
      if (failed) return;
      size += chunk.length;
      if (size > limit) {
        failed = true;
        // 停止累计，剩下的一律丢弃（resume 后没有 data 监听器，数据被直接丢掉）
        req.removeAllListeners('data');
        req.resume();
        reject(new Error('body_too_large'));
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      if (!failed) resolve(Buffer.concat(chunks));
    });
    req.on('error', (error) => {
      if (!failed) {
        failed = true;
        reject(error);
      }
    });
  });
}

/**
 * 读取并解析 JSON body。
 * 参数 req：请求；limit：字节上限。空 body 返回 {}，非法 JSON 抛 'invalid_json'。
 */
export async function read_json(req: IncomingMessage, limit = 8 * 1024 * 1024): Promise<any> {
  const buf = await read_body(req, limit);
  const text = buf.toString('utf8').trim();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    throw new Error('invalid_json');
  }
}

/**
 * 回 JSON。
 * 参数 res：响应；status：状态码；body：任意可序列化对象。
 */
export function send_json(res: ServerResponse, status: number, body: unknown): void {
  const text = JSON.stringify(body);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(text),
    'cache-control': 'no-store',
  });
  res.end(text);
}

/**
 * 回原始字节（TTS 音频等）。
 * 参数 res：响应；status：状态码；content_type：MIME；body：字节。
 */
export function send_bytes(res: ServerResponse, status: number, content_type: string, body: Buffer): void {
  res.writeHead(status, {
    'content-type': content_type || 'application/octet-stream',
    'content-length': body.length,
    'cache-control': 'no-store',
  });
  res.end(body);
}

/**
 * 回统一格式的错误。
 * 参数 res：响应；status：状态码；code：机器可读错误码；message：人读说明。
 */
export function send_error(res: ServerResponse, status: number, code: string, message: string): void {
  send_json(res, status, { error: { code, message } });
}

/**
 * 若 Content-Length 已经超过上限，直接回 413 并断开，不去读请求体。
 * 参数 req/res：请求与响应；limit：字节上限；what：错误信息里的对象名。
 * 返回 true 表示已经处理完（调用方应立刻 return）。
 *
 * 为什么要先看 Content-Length：33MB 的包如果边收边判断，等发现超限时
 * 已经吃过几十 MB 内存，而且直接 destroy 会让客户端只看到「连接被重置」，
 * 分不清是自己包太大还是网断了。提前回 413 能给个明确结论。
 */
export function reject_if_too_large(req: IncomingMessage, res: ServerResponse, limit: number, what = '请求体'): boolean {
  const raw = req.headers['content-length'];
  if (!raw) return false;
  const size = Number(raw);
  if (!Number.isFinite(size) || size <= limit) return false;
  res.setHeader('connection', 'close');
  send_error(
    res,
    413,
    'payload_too_large',
    `${what} ${(size / 1048576).toFixed(1)}MB 超过上限 ${Math.round(limit / 1048576)}MB`,
  );
  res.on('finish', () => {
    // 等一拍再断：立刻 destroy 会把还在内核缓冲里的响应 RST 掉，
    // 客户端（尤其是一次性 write 完整个 body 的）就只能看到「连接被重置」。
    setTimeout(() => {
      try {
        req.destroy();
      } catch {
        /* ignore */
      }
    }, 100).unref();
  });
  return true;
}

/** SSE 响应头（OpenAI 流式用）。 */
export function send_sse_headers(res: ServerResponse): void {
  res.writeHead(200, {
    'content-type': 'text/event-stream; charset=utf-8',
    'cache-control': 'no-cache, no-transform',
    connection: 'keep-alive',
    'x-accel-buffering': 'no',
  });
  // 先发一个注释行，让客户端立刻知道流已经建立
  res.write(': relay stream open\n\n');
}

/** 发一条 SSE data 行。 */
export function send_sse(res: ServerResponse, data: unknown): void {
  const text = typeof data === 'string' ? data : JSON.stringify(data);
  res.write(`data: ${text}\n\n`);
}

/** 结束 SSE 流（OpenAI 约定的 [DONE]）。 */
export function send_sse_done(res: ServerResponse): void {
  res.write('data: [DONE]\n\n');
  res.end();
}

/**
 * 令牌校验。
 * 参数 req：请求；query：查询串；expected：期望令牌。
 * 通过条件：Authorization: Bearer <token> 或 ?token=<token>。
 * expected 为空时视为「未设置令牌」，直接放行（开发模式，Hub 启动时会告警）。
 */
export function token_ok(req: IncomingMessage, query: URLSearchParams, expected: string): boolean {
  if (!expected) return true;
  const header = String(req.headers.authorization || '');
  const bearer = header.toLowerCase().startsWith('bearer ') ? header.slice(7).trim() : '';
  const fromQuery = query.get('token') || '';
  return bearer === expected || fromQuery === expected;
}

/**
 * 从请求头里取客户端 IP（Caddy 反代时读 x-forwarded-for 第一段）。
 * 参数 req：请求。
 */
export function client_ip(req: IncomingMessage): string {
  const forwarded = String(req.headers['x-forwarded-for'] || '').split(',')[0].trim();
  return forwarded || req.socket.remoteAddress || 'unknown';
}
