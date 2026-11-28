/**
 * 节点代理：连 Hub 的长连接客户端（心跳、重连、接单、分片回传）。
 *
 * 负责的功能：
 * - `start` / `stop`：建立与关闭长连接（断线自动重连，指数退避到 reconnectMaxMs）；
 * - 握手：连上后立刻发 hello（节点 id、名字、能力清单），等 hello.ok；
 * - 心跳：每 heartbeatSeconds 发 ping；连续 3 个周期收不到 Hub 任何帧就主动断开重连；
 * - 接单：收到 task 帧 → 如果声明了上行附件就先收齐分片 → 调用 capabilityRunner 执行；
 * - 回传：`emit_chunk` 按 aidx 维护递增 seq，最后一片带 last=true；
 * - 取消：收到 cancel 帧把任务标记为 cancelled，处理函数通过 `cancelled()` 提前退出；
 * - 排水：收到 drain 帧后本连接不再接新任务（重连时把 draining 一并带上）。
 *
 * 为什么上行附件要等收齐再执行：ASR 必须拿到完整音频才能转写；
 * 分片到达顺序由 WS 保证，但仍按 seq 校验，丢片就等待超时后判失败，而不是拿半截音频去转写。
 *
 * 注册参数（构造）：
 *   cfg     node_config（地址、令牌、心跳、重连参数）
 *   runner  capabilityRunner（已注册好本机能力）
 *   log     logger 实例
 */

import os from 'node:os';
import WebSocket from 'ws';
import type { node_config } from '../common/configLoader';
import type { logger } from '../common/logger';
import { decode_binary, decode_text, encode_binary } from '../protocol/frames';
import type { binary_header, relay_frame, upload_spec } from '../protocol/types';
import type { capabilityRunner } from './capabilityRunner';
import { RelayObservationStore } from '../common/RelayObservationStore';

/** 一个上行附件的接收状态。 */
interface upload_part {
  mimeType: string;
  kind: string;
  chunks: Buffer[];
  bytes: number;
  expect_seq: number;
  closed: boolean;
}

/** 一次任务的上行附件等待器。 */
interface upload_waiter {
  decls: upload_spec[];
  parts: Map<number, upload_part>;
  promise: Promise<Array<{ mimeType: string; kind: string; data: Buffer }>>;
  resolve: (value: Array<{ mimeType: string; kind: string; data: Buffer }>) => void;
  reject: (error: Error) => void;
  timer: NodeJS.Timeout | null;
}

/** 单个任务在本节点上的运行状态。 */
interface task_state {
  /** 派单代数（Hub 每次重派会自增，回传时要带上） */
  generation: number;
  /** 是否已收到取消指令 */
  cancelled: boolean;
  /** 每个附件下一个要发的 seq */
  seq: Map<number, number>;
  /** 每个附件已发片数（用于日志） */
  sent: Map<number, number>;
  /** 上行附件等待器（没有上行附件时为 null） */
  uploads: upload_waiter | null;
}

export class nodeAgent {
  private cfg: node_config;
  private runner: capabilityRunner;
  private log: logger;
  private ws: WebSocket | null = null;
  private stopped = false;
  private reconnect_delay: number;
  private reconnect_timer: NodeJS.Timeout | null = null;
  private heartbeat_timer: NodeJS.Timeout | null = null;
  private last_seen = 0;
  private draining = false;
  private tasks = new Map<string, task_state>();
  /** 最近一次成功握手的时刻（给运维看） */
  private connected_at = 0;
  /** 累计接单数（给 /v1/nodes 的 facts 用） */
  private handled = 0;

  constructor(cfg: node_config, runner: capabilityRunner, log: logger) {
    this.cfg = cfg;
    this.runner = runner;
    this.log = log;
    this.reconnect_delay = Math.max(200, cfg.reconnectMinMs);
  }

  /** 建立连接（失败会自动重连，不会抛）。 */
  start(): void {
    this.stopped = false;
    this.connect();
  }

  /** 主动关闭（不再重连）。 */
  stop(): void {
    this.stopped = true;
    if (this.reconnect_timer) clearTimeout(this.reconnect_timer);
    this.reconnect_timer = null;
    this.stop_heartbeat();
    for (const state of this.tasks.values()) {
      state.cancelled = true;
      if (state.uploads) {
        try {
          state.uploads.reject(new Error('node_stopped'));
        } catch {
          /* ignore */
        }
      }
    }
    this.tasks.clear();
    if (this.ws) {
      try {
        this.ws.close(1000, 'node_stop');
      } catch {
        /* ignore */
      }
      this.ws = null;
    }
    this.log.info('节点已停止');
  }

  /** 是否已连上 Hub。 */
  connected(): boolean {
    return Boolean(this.ws) && this.ws?.readyState === WebSocket.OPEN;
  }

  /** 当前正在跑的任务数。 */
  running_tasks(): number {
    return this.tasks.size;
  }

  /** 连接信息（给运维/测试看）。 */
  status(): Record<string, unknown> {
    return {
      nodeId: this.cfg.nodeId,
      name: this.cfg.name,
      hubUrl: this.cfg.hubUrl,
      connected: this.connected(),
      connectedAt: this.connected_at || null,
      running: this.tasks.size,
      handled: this.handled,
      draining: this.draining,
      capabilities: this.runner.specs(),
    };
  }

  /**
   * 模拟断线（冒烟测试用来验证「节点掉线 → Hub 重派」）。
   * 参数：无。会走正常的 close 流程并自动重连。
   */
  simulate_drop(): void {
    if (this.ws) {
      this.log.warn('手动断开连接（模拟掉线）');
      try {
        this.ws.terminate();
      } catch {
        /* ignore */
      }
    }
  }

  // ---------------------------------------------------------------- 连接

  /** 建连接并挂事件。 */
  private connect(): void {
    if (this.stopped) return;
    const url = this.with_token(this.cfg.hubUrl);
    this.log.info(`连接 Hub ${this.cfg.hubUrl}`);
    const ws = new WebSocket(url, { handshakeTimeout: 15000 });
    this.ws = ws;

    ws.on('open', () => {
      this.send({
        type: 'hello',
        role: 'node',
        nodeId: this.cfg.nodeId,
        name: this.cfg.name,
        capabilities: this.runner.specs(),
        draining: this.draining,
        heartbeatSeconds: this.cfg.heartbeatSeconds,
        facts: {
          host: os.hostname(),
          platform: process.platform,
          pid: process.pid,
          version: '1.0.0',
        },
      });
      this.last_seen = Date.now();
      this.start_heartbeat();
    });

    ws.on('message', (data: WebSocket.RawData, is_binary: boolean) => {
      this.last_seen = Date.now();
      try {
        if (is_binary) {
          this.handle_binary(data);
          return;
        }
        const frame = decode_text(data.toString());
        if (!frame) {
          this.log.warn('收到无法解析的文本帧，已忽略');
          return;
        }
        this.handle_frame(frame);
      } catch (error) {
        this.log.error(`处理 Hub 帧异常：${(error as Error).message}`);
      }
    });

    ws.on('close', (code: number, reason: Buffer) => {
      this.stop_heartbeat();
      this.ws = null;
      this.fail_pending_uploads('connection_closed');
      this.log.warn(`与 Hub 断开（${code} ${reason.toString() || ''}）`);
      this.schedule_reconnect();
    });

    ws.on('error', (error: Error) => {
      this.log.warn(`连接错误：${error.message}`);
    });

    ws.on('unexpected-response', (_req, res) => {
      this.log.error(`Hub 拒绝了连接：HTTP ${res.statusCode}（检查令牌与地址）`);
    });
  }

  /**
   * 把令牌拼到 URL 上（`?token=`），已带 token 参数时不重复拼。
   * 参数 url：配置里的 WS 地址。
   */
  private with_token(url: string): string {
    if (!this.cfg.token) return url;
    try {
      const parsed = new URL(url);
      if (!parsed.searchParams.get('token')) parsed.searchParams.set('token', this.cfg.token);
      return parsed.toString();
    } catch {
      const tail = url.includes('?') ? '&' : '?';
      return `${url}${tail}token=${encodeURIComponent(this.cfg.token)}`;
    }
  }

  /** 安排重连（指数退避，成功握手后复位）。 */
  private schedule_reconnect(): void {
    if (this.stopped) return;
    const delay = this.reconnect_delay;
    this.reconnect_delay = Math.min(this.cfg.reconnectMaxMs, Math.round(this.reconnect_delay * 1.6));
    this.log.info(`${delay}ms 后重连`);
    if (this.reconnect_timer) clearTimeout(this.reconnect_timer);
    this.reconnect_timer = setTimeout(() => {
      this.reconnect_timer = null;
      this.connect();
    }, delay);
    // 同样不能 unref：连接断开的瞬间如果只有这个定时器还活着，unref 会让进程直接退出，
    // 表现就是「掉线后再也不重连」，而日志里只剩一行「xxxms 后重连」。
  }

  /** 开启心跳：定时发 ping，长期收不到帧就断开重连。 */
  private start_heartbeat(): void {
    this.stop_heartbeat();
    const period = Math.max(1000, this.cfg.heartbeatSeconds * 1000);
    // 假活判定的下限给到 30 秒：传大文件时节点要连续吐/收几十 MB 二进制帧，
    // 期间收不到控制帧是正常的，用 3×心跳（心跳 1s 时只有 3s）会误杀自己。
    const dead_after = Math.max(period * 3, 30000);
    this.heartbeat_timer = setInterval(() => {
      if (!this.connected()) return;
      if (Date.now() - this.last_seen > dead_after) {
        this.log.warn(`${dead_after}ms 没收到 Hub 任何帧，判定连接假活，断开重连`);
        try {
          this.ws?.terminate();
        } catch {
          /* ignore */
        }
        return;
      }
      this.send({ type: 'ping', ts: Date.now() });
    }, period);
    // 注意：这里不能 unref。心跳是节点进程唯一的常驻句柄之一，
    // unref 之后连接一断、重连定时器又还没到点，事件循环就空了，进程会静默退出。
  }

  /** 停心跳。 */
  private stop_heartbeat(): void {
    if (this.heartbeat_timer) clearInterval(this.heartbeat_timer);
    this.heartbeat_timer = null;
  }

  // ---------------------------------------------------------------- 帧处理

  /**
   * 处理控制帧。
   * 参数 frame：Hub 发来的帧。
   */
  private handle_frame(frame: relay_frame): void {
    switch (frame.type) {
      case 'hello.ok':
        this.connected_at = Date.now();
        this.reconnect_delay = Math.max(200, this.cfg.reconnectMinMs);
        this.log.info(`握手成功（nodeId=${frame.nodeId}，心跳 ${frame.heartbeatSeconds}s）`);
        return;
      case 'ping':
        this.send({ type: 'pong', ts: frame.ts ?? Date.now() });
        return;
      case 'pong':
        return;
      case 'task':
        this.accept_task(frame);
        return;
      case 'cancel': {
        const task_id = String(frame.taskId || '');
        const state = this.tasks.get(task_id);
        if (state) {
          state.cancelled = true;
          this.log.warn(`任务 ${task_id} 被取消（${frame.reason || 'no reason'}）`);
        }
        return;
      }
      case 'drain':
        this.draining = frame.on !== false;
        this.log.info(`收到排水指令：${this.draining ? '停止接新任务' : '恢复接单'}`);
        return;
      default:
        this.log.debug(`未知帧类型 ${frame.type}`);
    }
  }

  /**
   * 接单：登记状态 → ack → 异步执行。
   * 参数 frame：task 帧。
   */
  private accept_task(frame: relay_frame): void {
    const task_id = String(frame.taskId || '');
    const capability = String(frame.capability || '');
    const generation = Number.isFinite(frame.generation) ? Number(frame.generation) : 0;
    if (!task_id) return;
    if (this.draining) {
      this.send({ type: 'ack', taskId: task_id, generation, accepted: false, reason: 'draining' });
      return;
    }
    if (!this.runner.has(capability)) {
      this.send({ type: 'ack', taskId: task_id, generation, accepted: false, reason: `no_capability:${capability}` });
      return;
    }
    if (this.tasks.has(task_id)) {
      this.send({ type: 'ack', taskId: task_id, generation, accepted: true });
      return;
    }

    const state: task_state = {
      generation,
      cancelled: false,
      seq: new Map(),
      sent: new Map(),
      uploads: null,
    };
    this.tasks.set(task_id, state);
    const decls = Array.isArray(frame.uploads) ? (frame.uploads as upload_spec[]) : [];
    if (decls.length) state.uploads = this.create_upload_waiter(decls);

    this.send({ type: 'ack', taskId: task_id, generation, accepted: true });
    this.log.info(`接单 ${task_id}（${capability}${decls.length ? `，等 ${decls.length} 个上行附件` : ''}）`);
    void this.execute_task(frame, state);
  }

  /**
   * 执行任务：等上行附件 → 调处理函数 → 回结果。
   * 参数 frame：task 帧；state：任务状态。
   */
  private async execute_task(frame: relay_frame, state: task_state): Promise<void> {
    const task_id = String(frame.taskId || '');
    const capability = String(frame.capability || '');
    const input = frame.input && typeof frame.input === 'object' ? (frame.input as Record<string, any>) : {};
    const timeout_ms = Number.isFinite(frame.timeoutMs) ? Number(frame.timeoutMs) : 600000;
    const trace = frame.meta && typeof frame.meta === 'object' ? frame.meta as Record<string, unknown> : {};
    const trace_id = /^[a-f0-9]{32}$/.test(String(trace.traceId ?? '')) ? String(trace.traceId) : undefined;
    const started = Date.now();
    let observed_status = 'error';
    if (trace_id) this.send({ type: 'observation', taskId: task_id, generation: state.generation, phase: 'start' });

    try {
      let uploads: Array<{ mimeType: string; kind: string; data: Buffer }> = [];
      if (state.uploads) {
        uploads = await state.uploads.promise;
      }
      if (state.cancelled) {
        this.finish_task(task_id, { ok: false, code: 'cancelled', error: '任务已取消' });
        return;
      }

      const result = await this.runner.run({
        traceparent: trace_id ? `00-${trace_id}-${RelayObservationStore.span_id(`${task_id}:node:${state.generation}`)}-01` : undefined,
        taskId: task_id,
        capability,
        input,
        uploads,
        emit_chunk: (chunk, last, meta) => this.emit_chunk(task_id, chunk, last, meta),
        progress: (percent, note, delta) => {
          if (!this.connected()) return;
          this.send({ type: 'progress', taskId: task_id, generation: state.generation, percent, note, delta });
        },
        cancelled: () => state.cancelled,
        log: this.log.child(task_id.slice(-6)),
      });

      if (state.cancelled && !result.error) {
        this.finish_task(task_id, { ok: false, code: 'cancelled', error: '任务已取消' });
        return;
      }
      if (result.error) {
        this.finish_task(task_id, { ok: false, code: 'handler_error', error: result.error });
        return;
      }
      observed_status = 'ok';
      this.finish_task(task_id, {
        ok: true,
        payload: result.payload ?? null,
        attachments: [...state.sent.entries()].map(([aidx, count]) => ({ aidx, chunks: count })),
      });
    } catch (error) {
      const message = (error as Error).message || 'unknown';
      this.log.error(`任务 ${task_id} 执行失败：${message}`);
      this.finish_task(task_id, { ok: false, code: 'node_exception', error: message });
    } finally {
      if (trace_id) this.send({
        type: 'observation', taskId: task_id, generation: state.generation, phase: 'end',
        status: state.cancelled ? 'cancelled' : observed_status, durationMs: Date.now() - started,
      });
      if (timeout_ms > 0) {
        // timeoutMs 由 Hub 传下来，节点侧不再单独计时（Hub 有全局判定），这里只是留个记号
      }
    }
  }

  /**
   * 回传一片二进制（自动维护 seq）。
   * 参数 task_id：任务 id；chunk：字节；last：是否最后一片；meta：附件信息。
   */
  private emit_chunk(task_id: string, chunk: Buffer, last: boolean, meta?: { aidx?: number; mimeType?: string; kind?: string }): void {
    const state = this.tasks.get(task_id);
    const aidx = Math.max(0, meta?.aidx ?? 0);
    const seq = state?.seq.get(aidx) ?? 0;
    if (state) {
      state.seq.set(aidx, seq + 1);
      state.sent.set(aidx, (state.sent.get(aidx) || 0) + 1);
    }
    const header: binary_header = {
      taskId: task_id,
      dir: 'down',
      aidx,
      seq,
      last,
      mimeType: meta?.mimeType || 'application/octet-stream',
      kind: meta?.kind || 'file',
      size: chunk.length,
    };
    this.send_binary(header, chunk);
  }

  /**
   * 上报结果并清理任务状态。
   * 参数 task_id：任务 id；body：result 帧除 type/taskId 外的字段。
   */
  private finish_task(task_id: string, body: Record<string, unknown>): void {
    const state = this.tasks.get(task_id);
    const generation = state?.generation ?? 0;
    if (state?.uploads) {
      if (state.uploads.timer) clearTimeout(state.uploads.timer);
      state.uploads = null;
    }
    this.tasks.delete(task_id);
    this.handled += 1;
    this.send({ type: 'result', taskId: task_id, generation, ...body });
    this.send({ type: 'node.stats', running: this.tasks.size, queued: 0 });
  }

  // ---------------------------------------------------------------- 上行附件

  /**
   * 建上行附件等待器（含超时）。
   * 参数 decls：Hub 声明的附件清单。
   */
  private create_upload_waiter(decls: upload_spec[]): upload_waiter {
    let resolve!: (value: Array<{ mimeType: string; kind: string; data: Buffer }>) => void;
    let reject!: (error: Error) => void;
    const promise = new Promise<Array<{ mimeType: string; kind: string; data: Buffer }>>((res, rej) => {
      resolve = res;
      reject = rej;
    });
    const waiter: upload_waiter = {
      decls,
      parts: new Map(),
      promise,
      resolve,
      reject,
      timer: null,
    };
    // 上行附件必须在一定时间内收齐，否则别把任务吊死
    waiter.timer = setTimeout(() => {
      reject(new Error('upload_timeout: 上行附件没有收齐'));
    }, 120000);
    if (typeof waiter.timer.unref === 'function') waiter.timer.unref();
    const total = decls.reduce((sum, item) => sum + (item.size || 0), 0);
    this.log.debug(`等待上行附件：${decls.length} 个，共 ${total} 字节`);
    return waiter;
  }

  /**
   * 处理上行分片。
   * 参数 payload：原始二进制。
   */
  private handle_binary(payload: WebSocket.RawData): void {
    const decoded = decode_binary(payload);
    if (!decoded) {
      this.log.warn('收到无法解析的二进制帧，已忽略');
      return;
    }
    const { header, body } = decoded;
    if (header.dir !== 'up') {
      this.log.warn(`收到 dir=${header.dir} 的二进制帧，节点只接受 up`);
      return;
    }
    const state = this.tasks.get(header.taskId);
    if (!state || !state.uploads) return;
    const waiter = state.uploads;
    const decl = waiter.decls.find((item) => item.aidx === header.aidx);
    if (!decl) {
      this.log.warn(`任务 ${header.taskId} 收到未声明的附件 ${header.aidx}`);
      return;
    }
    let part = waiter.parts.get(header.aidx);
    if (!part) {
      part = {
        mimeType: decl.mimeType || 'application/octet-stream',
        kind: decl.kind || 'file',
        chunks: [],
        bytes: 0,
        expect_seq: 0,
        closed: false,
      };
      waiter.parts.set(header.aidx, part);
    }
    if (header.seq !== part.expect_seq) {
      this.log.warn(`任务 ${header.taskId} 附件 ${header.aidx} 分片乱序（期望 ${part.expect_seq} 收到 ${header.seq}），丢弃该片`);
      return;
    }
    part.chunks.push(body);
    part.bytes += body.length;
    part.expect_seq += 1;
    if (header.last) part.closed = true;
    this.check_uploads_complete(header.taskId, waiter);
  }

  /**
   * 检查上行附件是否收齐；收齐就唤醒执行流程。
   * 参数 task_id：任务 id；waiter：等待器。
   */
  private check_uploads_complete(task_id: string, waiter: upload_waiter): void {
    const all_closed = waiter.decls.every((decl) => waiter.parts.get(decl.aidx)?.closed);
    if (!all_closed) return;
    if (waiter.timer) clearTimeout(waiter.timer);
    const ordered = waiter.decls
      .slice()
      .sort((a, b) => a.aidx - b.aidx)
      .map((decl) => {
        const part = waiter.parts.get(decl.aidx);
        return {
          mimeType: part?.mimeType || decl.mimeType,
          kind: part?.kind || decl.kind || 'file',
          data: part ? Buffer.concat(part.chunks) : Buffer.alloc(0),
        };
      });
    this.log.debug(`任务 ${task_id} 上行附件收齐（${ordered.map((item) => item.data.length).join(',')} 字节）`);
    waiter.resolve(ordered);
  }

  /** 连接断开时让所有等待上行附件的任务尽快失败（而不是吊到超时）。参数 reason：原因。 */
  private fail_pending_uploads(reason: string): void {
    for (const state of this.tasks.values()) {
      state.cancelled = true;
      if (state.uploads) {
        try {
          state.uploads.reject(new Error(reason));
        } catch {
          /* ignore */
        }
      }
    }
  }

  // ---------------------------------------------------------------- 发送

  /**
   * 发文本帧。
   * 参数 frame：帧对象。
   */
  private send(frame: relay_frame): void {
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    try {
      ws.send(JSON.stringify(frame));
    } catch (error) {
      this.log.debug(`发送失败：${(error as Error).message}`);
    }
  }

  /**
   * 发二进制帧。
   * 参数 header：帧头；body：字节。
   */
  private send_binary(header: binary_header, body: Buffer): void {
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    try {
      ws.send(encode_binary(header, body), { binary: true });
    } catch (error) {
      this.log.warn(`回传分片失败：${(error as Error).message}`);
    }
  }
}
