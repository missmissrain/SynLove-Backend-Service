/**
 * 统一日志器：控制台 + 可选文件双写，写文件失败只降级不抛异常。
 *
 * 负责的功能：
 * - 按级别过滤（debug/info/warn/error），带毫秒时间戳与来源标签；
 * - 同一进程内可以 `child()` 出带子标签的日志器（如 hub.dispatch），共享同一个文件队列；
 * - 文件写入用 `appendFile` 排队逐行落盘（**不缓冲**）：进程被强杀时已经写下的行不会丢，
 *   这对「VPS 上服务突然被杀」的排障很关键（用 WriteStream 时缓冲区会一起消失，实测踩过）；
 * - 写文件失败（被占用、权限、磁盘满）只告警并继续尝试，**绝不让日志把进程带崩**
 *   （2026-10-29 技能复盘就是被一次 PermissionError 带崩的，这是硬约束）；
 * - 附加对象序列化后截断（默认 800 字），避免一条日志塞进几十 MB 的音频。
 *
 * 注册参数（构造）：
 *   scope       日志来源标签，例如 'hub'
 *   opts.level  最低输出级别，默认 'info'
 *   opts.file   日志文件路径，默认 null（只打控制台）
 */

import fs from 'node:fs';
import path from 'node:path';

/** 日志级别。rank 越小越详细。 */
export type log_level = 'debug' | 'info' | 'warn' | 'error';

const RANK: Record<log_level, number> = { debug: 10, info: 20, warn: 30, error: 40 };

/** 文件写入的共享状态（父日志器与 child 共用，保证行不会互相交错）。 */
interface file_sink {
  file: string;
  /** 串行写入队列（每行一次 appendFile，写完即落盘） */
  queue: Promise<void>;
  /** 连续失败次数（用于限制告警频率） */
  fail_count: number;
}

export class logger {
  private scope: string;
  private min_rank: number;
  private sink: file_sink | null = null;

  constructor(scope: string, opts: { level?: log_level; file?: string | null } = {}) {
    this.scope = scope;
    this.min_rank = RANK[opts.level || 'info'];
    if (opts.file) this.open_file(opts.file);
  }

  /**
   * 派生一个子日志器（共享文件队列）。
   * 参数 sub：子标签，最终显示为 `父.子`。
   */
  child(sub: string): logger {
    const next = new logger(`${this.scope}.${sub}`, { level: 'info' });
    next.min_rank = this.min_rank;
    next.sink = this.sink;
    return next;
  }

  /** 当前最低级别名。 */
  level_name(): log_level {
    if (this.min_rank <= RANK.debug) return 'debug';
    if (this.min_rank <= RANK.info) return 'info';
    if (this.min_rank <= RANK.warn) return 'warn';
    return 'error';
  }

  /**
   * 准备日志目录并记下文件路径（真正写盘在每条日志时进行）。
   * 参数 file：绝对或相对路径。
   */
  private open_file(file: string): void {
    try {
      const dir = path.dirname(file);
      if (dir && dir !== '.') fs.mkdirSync(dir, { recursive: true });
      this.sink = { file, queue: Promise.resolve(), fail_count: 0 };
    } catch (error) {
      process.stderr.write(`[logger] 无法准备日志目录，改为只打控制台：${file}（${(error as Error).message}）\n`);
      this.sink = null;
    }
  }

  /** 调试日志（默认不输出，排查协议细节时把 logLevel 调成 debug）。 */
  debug(message: string, extra?: unknown): void {
    this.write_line('debug', message, extra);
  }

  /** 普通信息日志。 */
  info(message: string, extra?: unknown): void {
    this.write_line('info', message, extra);
  }

  /** 警告日志（可恢复的问题）。 */
  warn(message: string, extra?: unknown): void {
    this.write_line('warn', message, extra);
  }

  /** 错误日志（任务失败、节点掉线、上游不可用）。 */
  error(message: string, extra?: unknown): void {
    this.write_line('error', message, extra);
  }

  /**
   * 真正落一行的位置。
   * 参数 level：级别；message：正文；extra：附加对象（会 JSON 序列化并截断）。
   */
  private write_line(level: log_level, message: string, extra?: unknown): void {
    if (RANK[level] < this.min_rank) return;
    let tail = '';
    if (extra !== undefined) {
      try {
        const text = typeof extra === 'string' ? extra : JSON.stringify(extra);
        tail = ' ' + (text.length > 800 ? text.slice(0, 800) + '…' : text);
      } catch {
        tail = ' [附加对象无法序列化]';
      }
    }
    const line = `[${timestamp()}] [${level.toUpperCase()}] [${this.scope}] ${message}${tail}\n`;
    try {
      process.stdout.write(line);
    } catch {
      /* 控制台都写不了就只能放弃，但不能抛 */
    }
    this.append_file(line);
  }

  /**
   * 排队写文件（失败只告警）。
   * 参数 line：完整一行（含换行）。
   */
  private append_file(line: string): void {
    const sink = this.sink;
    if (!sink) return;
    sink.queue = sink.queue.then(async () => {
      try {
        await fs.promises.appendFile(sink.file, line, 'utf8');
        sink.fail_count = 0;
      } catch (error) {
        sink.fail_count += 1;
        if (sink.fail_count === 1 || sink.fail_count % 100 === 0) {
          process.stderr.write(`[logger] 日志写入失败第 ${sink.fail_count} 次（${(error as Error).message}），本条只留在控制台\n`);
        }
      }
    });
  }

  /**
   * 等队列里的日志写完（进程退出前调用）。
   * 返回 Promise，调用方 await 它即可；最多等 3 秒，避免磁盘卡住时退出不了。
   */
  async close(): Promise<void> {
    const sink = this.sink;
    if (!sink) return;
    await Promise.race([
      sink.queue.catch(() => undefined),
      new Promise<void>((resolve) => setTimeout(resolve, 3000)),
    ]);
  }
}

/** 本地时间戳，精确到毫秒。 */
function timestamp(): string {
  const d = new Date();
  const pad = (n: number, w = 2) => String(n).padStart(w, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
}
