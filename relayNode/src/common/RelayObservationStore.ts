/**
 * 中转观测缓存：保存有限的任务/节点执行元数据，不参与任务状态判定。
 * 注册参数 capacity：最多保留事件数，默认 4096；Hub 重启后缓存清空。
 * 不保存任务输入、输出或密钥；客户端按 eventId 去重导入自己的本地观测库。
 */
import { createHash, randomBytes } from 'node:crypto';
import type { IncomingMessage } from 'node:http';

export class RelayObservationStore {
  private events: Array<Record<string, unknown>> = [];
  private seq = 0;
  private bootId = randomBytes(8).toString('hex');

  /** 参数 capacity：缓存的事件上限；有界内存不随长任务数量无限增长。 */
  constructor(private capacity = 4096) {}

  /** 参数 value：标识材料；返回 16 位 span ID，不包含业务正文。 */
  static span_id(value: string): string {
    return createHash('sha256').update(value).digest('hex').slice(0, 16);
  }

  /** 参数 req：已鉴权 HTTP 请求；只接受有效 W3C v00 traceparent，否则建立独立 trace。 */
  static request_meta(req: IncomingMessage): Record<string, unknown> {
    const raw = String(req.headers.traceparent ?? '');
    const match = /^00-([a-f0-9]{32})-([a-f0-9]{16})-[a-f0-9]{2}$/.exec(raw);
    const valid = match && !/^0+$/.test(match[1]) && !/^0+$/.test(match[2]);
    const session = String(req.headers['x-synlove-session-id'] ?? '').slice(0, 1200);
    let sessionId = session;
    try { sessionId = decodeURIComponent(session); } catch { /* 兼容未编码的旧客户端，不改变任务执行。 */ }
    return {
      traceId: valid ? match[1] : randomBytes(16).toString('hex'),
      parentSpanId: valid ? match[2] : undefined,
      sessionId: sessionId.slice(0, 200) || undefined,
    };
  }

  /** 参数 record：内部已筛选元数据；缓存事件并分配跨分页去重标识。 */
  append(record: Record<string, unknown>): void {
    this.events.push({ ...record, source: 'relay', eventId: `${this.bootId}:${++this.seq}`, at: new Date().toISOString() });
    if (this.events.length > this.capacity) this.events.splice(0, this.events.length - this.capacity);
  }

  /** 参数 after：上次已读取 eventId；返回后续事件及是否有缓存缺口，不返回业务输入输出。 */
  read(after = ''): { events: Array<Record<string, unknown>>; cursor: string; gap: boolean } {
    const index = this.events.findIndex(item => item.eventId === after);
    const events = after && index >= 0 ? this.events.slice(index + 1) : this.events.slice();
    return { events, cursor: `${this.bootId}:${this.seq}`, gap: Boolean(after && index < 0 && after !== `${this.bootId}:${this.seq}`) };
  }
}
