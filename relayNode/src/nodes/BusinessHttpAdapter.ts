/**
 * 业务能力HTTP适配器。注册参数cfg只允许服务端固定的回环上游；客户端仅可选白名单路径。
 * 支持JSON、附件上传和结果附件，复用Hub的任务协议，不开放任意代理、shell或关机接口。
 */
import type { worker_config } from '../common/configLoader';
import type { capability_handler } from './capabilityRunner';

export class BusinessHttpAdapter {
  /** cfg为固定服务端配置，name为当前能力身份，默认请求超时30秒。 */
  constructor(private cfg: worker_config, private name: string) {}

  /** 无参数；返回可注册到能力容器的处理器，网络错误只影响本次任务。 */
  make_handler(): capability_handler {
    return async ctx => {
      const input = ctx.input || {};
      const method = String(input.method || 'GET');
      const path = String(input.path || '');
      const read = method === 'GET' && /^\/(?:health|v1\/(?:tools|snapshot|notifications|operations\/[a-zA-Z0-9_-]{1,100}|files\/[a-f0-9]{32}(?:\.[a-zA-Z0-9]{1,10})?))$/.test(path);
      const write = method === 'POST' && /^\/v1\/(?:rpc|files\?suffix=\.[a-zA-Z0-9]{1,10}|notifications\/[a-f0-9]{64}\/ack)$/.test(path);
      if (!read && !write) return { error: 'invalid_business_route' };
      const base = new URL(this.cfg.baseUrl || '');
      if (base.protocol !== 'http:' || !['localhost', '127.0.0.1', '[::1]'].includes(base.hostname) ||
          base.username || base.password) return { error: 'invalid_business_upstream' };
      const headers: Record<string, string> = { 'content-type': 'application/json' };
      const key = this.cfg.apiKeyEnv ? process.env[this.cfg.apiKeyEnv] : '';
      if (key) headers.authorization = `Bearer ${key}`;
      let body: string | Buffer | undefined;
      if (method === 'POST') {
        if (path.startsWith('/v1/files?')) {
          if (ctx.uploads.length !== 1) return { error: 'one_upload_required' };
          body = ctx.uploads[0].data;
          headers['content-type'] = 'application/octet-stream';
        } else body = JSON.stringify(input.body || {});
      }
      if (ctx.cancelled()) return { error: 'cancelled' };
      const res = await fetch(new URL(path, base.origin), {
        method, headers, body, redirect: 'error',
        signal: AbortSignal.timeout(this.cfg.timeoutMs || 30000),
      });
      // 限制读取总量，不能先arrayBuffer再检查而被超大响应耗尽内存。
      const chunks: Buffer[] = [];
      let size = 0;
      if (res.body) for await (const chunk of res.body as any) {
        size += chunk.length;
        if (size > 32 * 1024 * 1024) {
          throw new Error('business_response_too_large');
        }
        chunks.push(Buffer.from(chunk));
      }
      const data = Buffer.concat(chunks);
      if (path.startsWith('/v1/files/') && res.ok) {
        for (let offset = 0; offset < data.length; offset += 65536) {
          ctx.emit_chunk(data.subarray(offset, offset + 65536), offset + 65536 >= data.length,
                         { aidx: 0, mimeType: 'application/octet-stream', kind: 'file' });
        }
        return { payload: { status: res.status, binary: true, service: this.name } };
      }
      let json: unknown;
      try { json = JSON.parse(data.toString('utf8')); }
      catch { return { error: 'invalid_business_json' }; }
      return { payload: { status: res.status, body: json, service: this.name } };
    };
  }
}
