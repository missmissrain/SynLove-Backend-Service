/**
 * 本机或远程模型服务适配层：把兼容 HTTP 的 LLM/TTS/ASR 包成节点能力。
 *
 * 负责的功能：
 * - `make_chat_handler`：调 OpenAI 兼容的 `/chat/completions`，**始终向上游要流**，
 *   这样即使调用方没要求流式，也能把 token 增量通过 progress 帧转出去（WS 上边跑边看）；
 * - `make_tts_handler`：调用 `/audio/speech` 获取音频字节，
 *   把整段音频按 64KB 分片回传，避免一次性把几十 MB 塞进一个 WS 帧；
 * - `make_asr_handler`：把上行音频用 multipart 发给 whisper（或 OpenAI `/audio/transcriptions`）；
 * - `build_runner_from_config`：按 config/node.json 里的 workers 与 capabilities 组装 capabilityRunner，
 *   缺配置或模式不认识的能力会被跳过并记日志（不让节点因为一项配错就起不来）。
 *
 * 超时策略：每个上游请求都挂 `AbortSignal.timeout(worker.timeoutMs)`。
 * 上游超时/挂掉只会让这一个任务失败，节点继续接别的活。
 *
 * 注册参数（工厂函数）：
 *   cfg   worker_config（mode / baseUrl / model / voice / format / timeoutMs）
 *   log   logger 实例
 */

import type { logger } from '../common/logger';
import type { node_config, worker_config } from '../common/configLoader';
import { slice_chunks } from '../protocol/frames';
import { capabilityRunner, type capability_handler, type run_context } from './capabilityRunner';
import { BusinessHttpAdapter } from './BusinessHttpAdapter';

/** 流式增量攒多久推一次（毫秒）：太小会把 WS 打满，太大首字延迟高。 */
const DELTA_FLUSH_MS = 40;

/**
 * 造一个 chat 处理函数。
 * 参数 cfg：worker 配置；log：日志器。
 */
export function make_chat_handler(cfg: worker_config, log: logger): capability_handler {
  return async (ctx: run_context) => {
    const input = ctx.input || {};
    const messages = Array.isArray(input.messages) ? input.messages : [];
    if (!messages.length) return { error: 'empty_messages: 没有可发送的消息' };

    const base = trim_slash(cfg.baseUrl || 'http://127.0.0.1:8080/v1');
    const headers: Record<string, string> = { 'content-type': 'application/json' };
    if (ctx.traceparent) headers.traceparent = ctx.traceparent;
    const key = cfg.apiKeyEnv ? process.env[cfg.apiKeyEnv] : undefined;
    if (key) headers.authorization = `Bearer ${key}`;

    const model = String(input.model || cfg.model || 'default');
    const body: Record<string, unknown> = {
      model,
      messages,
      stream: true,
      temperature: input.temperature,
      top_p: input.top_p,
      max_tokens: input.max_tokens,
      stop: input.stop,
    };
    for (const k of Object.keys(body)) {
      if (body[k] === undefined) delete body[k];
    }

    const timeout = Math.max(5000, cfg.timeoutMs ?? 180000);
    let response: Response;
    try {
      response = await fetch(`${base}/chat/completions`, {
        method: 'POST',
        redirect: 'error',
        headers,
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(timeout),
      });
    } catch (error) {
      return { error: `upstream_unreachable: ${(error as Error).message}` };
    }
    if (!response.ok) {
      const detail = await response.text().catch(() => '');
      return { error: `upstream_${response.status}: ${detail.slice(0, 300)}` };
    }

    const content_type = String(response.headers.get('content-type') || '');
    // 上游没按流回（有些 OpenAI 兼容实现会忽略 stream），直接按整包 JSON 处理
    if (content_type.includes('application/json')) {
      const data: any = await response.json().catch(() => ({}));
      const text = String(data?.choices?.[0]?.message?.content || '');
      if (!text) return { error: 'upstream_empty: 上游返回空内容' };
      ctx.progress(100, 'done');
      return { payload: { text, model: data?.model || model, usage: data?.usage } };
    }

    if (!response.body) return { error: 'upstream_no_body: 上游没有返回可读流' };

    let text = '';
    let pending = '';
    let last_flush = Date.now();
    let usage: unknown = undefined;
    let upstream_model = model;
    let buffer = '';

    const flush = (force = false) => {
      if (!pending) return;
      const now = Date.now();
      if (!force && now - last_flush < DELTA_FLUSH_MS) return;
      ctx.progress(undefined, undefined, pending);
      pending = '';
      last_flush = now;
    };

    try {
      for await (const chunk of response.body as unknown as AsyncIterable<Uint8Array>) {
        buffer += Buffer.from(chunk).toString('utf8');
        let cut = buffer.indexOf('\n');
        while (cut >= 0) {
          const line = buffer.slice(0, cut).trim();
          buffer = buffer.slice(cut + 1);
          cut = buffer.indexOf('\n');
          if (!line || !line.startsWith('data:')) continue;
          const payload = line.slice(5).trim();
          if (payload === '[DONE]') continue;
          let parsed: any;
          try {
            parsed = JSON.parse(payload);
          } catch {
            continue;
          }
          if (parsed?.model) upstream_model = String(parsed.model);
          if (parsed?.usage) usage = parsed.usage;
          const delta = parsed?.choices?.[0]?.delta?.content;
          if (typeof delta === 'string' && delta) {
            text += delta;
            pending += delta;
            flush();
          }
          if (ctx.cancelled()) {
            flush(true);
            return { error: 'cancelled: 调用方取消' };
          }
        }
      }
      flush(true);
    } catch (error) {
      flush(true);
      if (text) {
        // 已经拿到部分内容：把半截结果交上去，比整条失败更有用
        log.warn(`任务 ${ctx.taskId} 上游流中断（${(error as Error).message}），返回已生成内容`);
        return { payload: { text, model: upstream_model, usage, partial: true } };
      }
      return { error: `upstream_stream_failed: ${(error as Error).message}` };
    }

    if (!text) return { error: 'upstream_empty: 上游没有产出任何文本' };
    ctx.progress(100, 'done');
    return { payload: { text, model: upstream_model, usage } };
  };
}

/**
 * 造一个 tts 处理函数。
 * 参数 cfg：worker 配置；log：日志器。
 */
export function make_tts_handler(cfg: worker_config, log: logger): capability_handler {
  return async (ctx: run_context) => {
    const input = ctx.input || {};
    const text = String(input.text || '');
    if (!text.trim()) return { error: 'empty_text: 合成文本为空' };
    const format = String(input.format || cfg.format || (cfg.mode === 'breeze' ? 'wav' : 'mp3'));
    const voice = String(input.voice || cfg.voice || 'default');

    const base = trim_slash(cfg.baseUrl || 'http://127.0.0.1:7862');
    const timeout = Math.max(5000, cfg.timeoutMs ?? 180000);
    const openai_mode = ['openai', 'openai-speech', 'breeze', 'tts'].includes(cfg.mode);

    const audio_base = cfg.mode === 'breeze' && !base.endsWith('/v1') ? `${base}/v1` : base;
    const url = openai_mode ? `${audio_base}/audio/speech` : `${base}/speak`;
    const payload = openai_mode
      ? { model: cfg.model || 'tts-1', input: text, voice, response_format: format, speed: input.speed }
      : { text, voice, format, speed: input.speed };

    let response: Response;
    try {
      response = await fetch(url, {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          ...(cfg.apiKeyEnv && process.env[cfg.apiKeyEnv] ? { authorization: `Bearer ${process.env[cfg.apiKeyEnv]}` } : {}),
          ...(ctx.traceparent ? { traceparent: ctx.traceparent } : {}),
        },
        redirect: 'error',
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(timeout),
      });
    } catch (error) {
      return { error: `upstream_unreachable: ${(error as Error).message}` };
    }
    if (!response.ok) {
      const detail = await response.text().catch(() => '');
      return { error: `upstream_${response.status}: ${detail.slice(0, 300)}` };
    }

    // 成功接口必须返回音频字节，不能把 JSON 路径或错误当成语音转发。
    const content_type = String(response.headers.get('content-type') || '');
    let audio: Buffer;
    if (!content_type.toLowerCase().startsWith('audio/')) {
      const data: any = await response.json().catch(() => ({}));
      return { error: `upstream_error: ${data?.error || data?.message || '上游返回 JSON 而不是音频'}` };
    }
    try {
      const reader = response.body?.getReader();
      if (!reader) return { error: 'empty_audio: 上游没有音频正文' };
      const parts: Buffer[] = [];
      let size = 0;
      try {
        while (true) {
          const item = await reader.read();
          if (item.done) break;
          size += item.value.byteLength;
          if (size > 32 * 1024 * 1024) {
            await reader.cancel();
            return { error: 'audio_too_large: 音频超过 32 MiB' };
          }
          parts.push(Buffer.from(item.value));
        }
      } finally {
        reader.releaseLock();
      }
      audio = Buffer.concat(parts, size);
    } catch (error) {
      return { error: `upstream_read_failed: ${(error as Error).message}` };
    }
    if (!audio.length) return { error: 'empty_audio: 上游返回 0 字节' };

    const mime = mime_of(format);
    const chunks = slice_chunks(audio, 64 * 1024);
    for (let index = 0; index < chunks.length; index += 1) {
      if (ctx.cancelled()) return { error: 'cancelled: 调用方取消' };
      ctx.emit_chunk(chunks[index], index === chunks.length - 1, { mimeType: mime, kind: 'audio' });
    }
    ctx.progress(100, 'done');
    log.debug(`任务 ${ctx.taskId} 合成 ${audio.length} 字节（${chunks.length} 片，${format}）`);
    return { payload: { bytes: audio.length, format, chunks: chunks.length } };
  };
}

/**
 * 造一个 asr 处理函数。
 * 参数 cfg：worker 配置；log：日志器。
 */
export function make_asr_handler(cfg: worker_config, log: logger): capability_handler {
  return async (ctx: run_context) => {
    const upload = ctx.uploads[0];
    if (!upload || !upload.data.length) return { error: 'no_audio: 没有收到音频' };
    const input = ctx.input || {};
    const base = trim_slash(cfg.baseUrl || 'http://127.0.0.1:8081');
    const timeout = Math.max(10000, cfg.timeoutMs ?? 300000);
    const openai_mode = ['openai', 'openai-whisper', 'whisper', 'asr'].includes(cfg.mode);

    const form = new FormData();
    const blob = new Blob([new Uint8Array(upload.data)], { type: upload.mimeType || 'audio/mpeg' });
    form.append('file', blob, filename_of(upload.mimeType));
    if (input.language) form.append('language', String(input.language));
    if (input.prompt) form.append('prompt', String(input.prompt));
    if (openai_mode) {
      form.append('model', String(input.model || cfg.model || 'whisper-1'));
    } else if (cfg.model) {
      form.append('model', String(cfg.model));
    }

    const audio_base = cfg.mode === 'whisper' && !base.endsWith('/v1') ? `${base}/v1` : base;
    const url = openai_mode ? `${audio_base}/audio/transcriptions` : `${base}/transcribe`;
    let response: Response;
    try {
      response = await fetch(url, {
        method: 'POST',
        headers: {
          ...(cfg.apiKeyEnv && process.env[cfg.apiKeyEnv] ? { authorization: `Bearer ${process.env[cfg.apiKeyEnv]}` } : {}),
          ...(ctx.traceparent ? { traceparent: ctx.traceparent } : {}),
        },
        redirect: 'error',
        body: form,
        signal: AbortSignal.timeout(timeout),
      });
    } catch (error) {
      return { error: `upstream_unreachable: ${(error as Error).message}` };
    }
    if (!response.ok) {
      const detail = await response.text().catch(() => '');
      return { error: `upstream_${response.status}: ${detail.slice(0, 300)}` };
    }
    const data: any = await response.json().catch(() => ({}));
    const text = typeof data?.text === 'string' ? data.text : (typeof data?.result === 'string' ? data.result : '');
    if (!text) return { error: 'upstream_empty: 上游没有给出转写文本' };
    ctx.progress(100, 'done');
    log.debug(`任务 ${ctx.taskId} 转写 ${upload.data.length} 字节 → ${text.length} 字`);
    return {
      payload: {
        text,
        language: data?.language || input.language || null,
        duration: data?.duration ?? null,
        segments: data?.segments,
      },
    };
  };
}

/**
 * 按配置组装 runner。
 * 参数 cfg：节点配置；log：日志器。
 * 返回 runner 与被跳过能力的原因（运维看日志就知道哪里配错了）。
 */
export function build_runner_from_config(cfg: node_config, log: logger): { runner: capabilityRunner; skipped: string[] } {
  const runner = new capabilityRunner();
  const skipped: string[] = [];

  for (const cap of cfg.capabilities) {
    const worker = cfg.workers[cap.name];
    if (!worker) {
      skipped.push(`${cap.name}（workers 里没有配置）`);
      continue;
    }
    const handler = build_handler(cap.name, worker, log);
    if (!handler) {
      skipped.push(`${cap.name}（不认识的 mode=${worker.mode}）`);
      continue;
    }
    runner.register(cap, handler);
    log.info(`注册能力 ${cap.name}：mode=${worker.mode} ，优先级 ${cap.priority}，并发 ${cap.concurrency}` +
      `${worker.baseUrl ? `，上游 ${worker.baseUrl}` : ''}`);
  }
  return { runner, skipped };
}

/**
 * 按能力名与 mode 选适配器。
 * 参数 name：能力名；worker：worker 配置；log：日志器。
 * 返回 null 表示这个 mode 不支持。
 */
function build_handler(name: string, worker: worker_config, log: logger): capability_handler | null {
  const mode = (worker.mode || '').toLowerCase();
  if (mode === 'synlove-business' && ['crawlerCenter', 'webCollection', 'scheduler', 'video'].includes(name)) {
    return new BusinessHttpAdapter(worker, name).make_handler();
  }
  if (mode === 'gemma' || mode === 'openai-chat' || mode === 'chat') return make_chat_handler(worker, log);
  if (mode === 'breeze' || mode === 'openai-speech' || mode === 'tts') return make_tts_handler(worker, log);
  if (mode === 'whisper' || mode === 'openai-whisper' || mode === 'asr') return make_asr_handler(worker, log);
  if (mode === 'openai') {
    // 泛化的 openai 模式：按能力名猜端点
    if (name === 'tts') return make_tts_handler({ ...worker, mode: 'openai' }, log);
    if (name === 'asr') return make_asr_handler({ ...worker, mode: 'openai' }, log);
    return make_chat_handler(worker, log);
  }
  return null;
}

/** 去掉 URL 末尾的斜杠。参数 url：原地址。 */
function trim_slash(url: string): string {
  return url.replace(/\/+$/, '');
}

/** 按格式给 MIME。参数 format：mp3 / wav / ogg / opus / pcm。 */
function mime_of(format: string): string {
  const key = format.toLowerCase();
  if (key === 'wav') return 'audio/wav';
  if (key === 'ogg') return 'audio/ogg';
  if (key === 'opus') return 'audio/opus';
  if (key === 'pcm') return 'audio/pcm';
  return 'audio/mpeg';
}

/** 按 MIME 给个像样的文件名（whisper 侧会按扩展名判断格式）。参数 mime：音频 MIME。 */
function filename_of(mime: string): string {
  const key = (mime || '').toLowerCase();
  if (key.includes('wav')) return 'input.wav';
  if (key.includes('ogg')) return 'input.ogg';
  if (key.includes('opus')) return 'input.opus';
  if (key.includes('webm')) return 'input.webm';
  if (key.includes('mp4') || key.includes('m4a')) return 'input.m4a';
  return 'input.mp3';
}
