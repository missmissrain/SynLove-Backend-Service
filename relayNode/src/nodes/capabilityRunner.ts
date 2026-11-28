/**
 * 节点侧能力容器：把「一个能力名」绑定到「一个处理函数」，并统一包住异常。
 *
 * 负责的功能：
 * - `register`：注册能力（能力名、优先级、并发上限）+ 处理函数；
 * - `specs`：导出能力清单，节点上线时放进 hello 帧；
 * - `run`：执行一次任务，把处理函数抛出的任何异常转成 `{error}`，
 *   保证「单个任务失败」永远不会把节点进程带崩（节点要在无人值守的机器上长期跑）；
 * - `has` / `names`：查询能力是否存在。
 *
 * 处理函数拿到的是 `run_context`：
 *   emit_chunk  回传一段二进制（TTS 边合成边发，不必等整段完成）
 *   progress    上报进度（percent / note / delta 增量文本）
 *   cancelled   查询是否已被取消（长任务应周期性检查并提前退出）
 *   uploads     上行附件（ASR 音频），已按 aidx 排好序
 *
 * 注册参数（构造）：无；能力通过 register 动态添加。
 */

import type { capability_spec } from '../protocol/types';
import type { logger } from '../common/logger';

/** 回传分片的选项。 */
export interface chunk_meta {
  /** 附件下标，默认 0 */
  aidx?: number;
  /** MIME 类型，默认 application/octet-stream */
  mimeType?: string;
  /** 用途标记，默认 file */
  kind?: string;
}

/** 处理函数拿到的上下文。 */
export interface run_context {
  /** 可选 W3C 追踪头，只传给本机模型服务，不进入模型输入。 */
  traceparent?: string;
  /** 任务 id */
  taskId: string;
  /** 能力名 */
  capability: string;
  /** 业务入参（HTTP 里传进来的 input） */
  input: Record<string, any>;
  /** 上行附件（ASR 音频），按声明顺序 */
  uploads: Array<{ mimeType: string; kind: string; data: Buffer }>;
  /** 回传一段二进制；last=true 表示该附件结束 */
  emit_chunk: (chunk: Buffer, last: boolean, meta?: chunk_meta) => void;
  /** 上报进度；delta 只用于流式文本 */
  progress: (percent?: number, note?: string, delta?: string) => void;
  /** 是否已被取消 */
  cancelled: () => boolean;
  /** 处理函数自己的日志器 */
  log: logger;
}

/** 处理函数返回值。error 非空表示任务失败。 */
export interface run_result {
  payload?: Record<string, any> | null;
  error?: string;
}

/** 处理函数签名。 */
export type capability_handler = (ctx: run_context) => Promise<run_result>;

/** 注册项。 */
interface registration {
  spec: capability_spec;
  handler: capability_handler;
}

export class capabilityRunner {
  private items = new Map<string, registration>();

  /**
   * 注册一个能力。
   * 参数 spec：能力描述（name 必填）；handler：处理函数。
   */
  register(spec: capability_spec, handler: capability_handler): void {
    if (!spec || !spec.name) throw new Error('capabilityRunner.register 需要能力名');
    this.items.set(spec.name, { spec, handler });
  }

  /** 是否注册了该能力。参数 name：能力名。 */
  has(name: string): boolean {
    return this.items.has(name);
  }

  /** 已注册的能力名列表。 */
  names(): string[] {
    return [...this.items.keys()];
  }

  /** 导出能力清单（放进 hello 帧）。 */
  specs(): capability_spec[] {
    return [...this.items.values()].map((item) => item.spec);
  }

  /**
   * 执行一次任务。
   * 参数 ctx：运行上下文。返回处理函数的结果；未注册能力或抛异常都转成 {error}。
   */
  async run(ctx: run_context): Promise<run_result> {
    const item = this.items.get(ctx.capability);
    if (!item) return { error: `capability_not_found: 节点没有注册 ${ctx.capability}` };
    try {
      const result = await item.handler(ctx);
      return result || {};
    } catch (error) {
      const message = (error as Error).message || 'unknown';
      ctx.log.error(`任务 ${ctx.taskId} 处理异常：${message}`);
      return { error: `${(error as Error).name || 'error'}: ${message}` };
    }
  }
}
