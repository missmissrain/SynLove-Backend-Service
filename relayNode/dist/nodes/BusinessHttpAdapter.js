"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.BusinessHttpAdapter = void 0;
class BusinessHttpAdapter {
    cfg;
    name;
    /** cfg为固定服务端配置，name为当前能力身份，默认请求超时30秒。 */
    constructor(cfg, name) {
        this.cfg = cfg;
        this.name = name;
    }
    /** 无参数；返回可注册到能力容器的处理器，网络错误只影响本次任务。 */
    make_handler() {
        return async (ctx) => {
            const input = ctx.input || {};
            const method = String(input.method || 'GET');
            const path = String(input.path || '');
            const read = method === 'GET' && /^\/(?:health|v1\/(?:tools|snapshot|notifications|operations\/[a-zA-Z0-9_-]{1,100}|files\/[a-f0-9]{32}(?:\.[a-zA-Z0-9]{1,10})?))$/.test(path);
            const write = method === 'POST' && /^\/v1\/(?:rpc|files\?suffix=\.[a-zA-Z0-9]{1,10}|notifications\/[a-f0-9]{64}\/ack)$/.test(path);
            if (!read && !write)
                return { error: 'invalid_business_route' };
            const base = new URL(this.cfg.baseUrl || '');
            if (base.protocol !== 'http:' || !['localhost', '127.0.0.1', '[::1]'].includes(base.hostname) ||
                base.username || base.password)
                return { error: 'invalid_business_upstream' };
            const headers = { 'content-type': 'application/json' };
            const key = this.cfg.apiKeyEnv ? process.env[this.cfg.apiKeyEnv] : '';
            if (key)
                headers.authorization = `Bearer ${key}`;
            let body;
            if (method === 'POST') {
                if (path.startsWith('/v1/files?')) {
                    if (ctx.uploads.length !== 1)
                        return { error: 'one_upload_required' };
                    body = ctx.uploads[0].data;
                    headers['content-type'] = 'application/octet-stream';
                }
                else
                    body = JSON.stringify(input.body || {});
            }
            if (ctx.cancelled())
                return { error: 'cancelled' };
            const res = await fetch(new URL(path, base.origin), {
                method, headers, body, redirect: 'error',
                signal: AbortSignal.timeout(this.cfg.timeoutMs || 30000),
            });
            // 限制读取总量，不能先arrayBuffer再检查而被超大响应耗尽内存。
            const chunks = [];
            let size = 0;
            if (res.body)
                for await (const chunk of res.body) {
                    size += chunk.length;
                    if (size > 32 * 1024 * 1024) {
                        throw new Error('business_response_too_large');
                    }
                    chunks.push(Buffer.from(chunk));
                }
            const data = Buffer.concat(chunks);
            if (path.startsWith('/v1/files/') && res.ok) {
                for (let offset = 0; offset < data.length; offset += 65536) {
                    ctx.emit_chunk(data.subarray(offset, offset + 65536), offset + 65536 >= data.length, { aidx: 0, mimeType: 'application/octet-stream', kind: 'file' });
                }
                return { payload: { status: res.status, binary: true, service: this.name } };
            }
            let json;
            try {
                json = JSON.parse(data.toString('utf8'));
            }
            catch {
                return { error: 'invalid_business_json' };
            }
            return { payload: { status: res.status, body: json, service: this.name } };
        };
    }
}
exports.BusinessHttpAdapter = BusinessHttpAdapter;
//# sourceMappingURL=BusinessHttpAdapter.js.map