/**
 * 极简 multipart/form-data 解析（只为 OpenAI 兼容的 /v1/audio/transcriptions 服务）。
 *
 * 负责的功能：
 * - 从 Content-Type 里取出 boundary；
 * - 把 body 切成若干 part，识别普通字段与文件字段（带 filename）；
 * - 二进制安全：全程在 Buffer 上操作，不做 utf8 中转（音频会被写坏）。
 *
 * 为什么自己写：为了不引入 multer/busboy 这类依赖，VPS 上依赖越少越好；
 * 而且我们只需要「单文件 + 几个文本字段」这一种用法，OpenAI 客户端发出来的就是这个形状。
 * 不支持：嵌套 multipart、base64 传输编码、超大文件流式解析（整体进内存，上限由调用方把关）。
 *
 * 注册参数：无（纯函数模块）。
 */

/** 解析出来的一个文件 part。 */
export interface multipart_file {
  /** 表单字段名，例如 file */
  field: string;
  /** 原始文件名，例如 a.mp3 */
  filename: string;
  /** 文件 MIME，例如 audio/mpeg */
  contentType: string;
  /** 文件字节 */
  data: Buffer;
}

/** 解析结果。 */
export interface multipart_result {
  fields: Record<string, string>;
  files: multipart_file[];
}

/**
 * 从 Content-Type 里取 boundary。
 * 参数 content_type：请求头；取不到返回 null。
 */
export function read_boundary(content_type: string): string | null {
  const match = /boundary=(?:"([^"]+)"|([^;]+))/i.exec(content_type || '');
  if (!match) return null;
  return (match[1] || match[2] || '').trim() || null;
}

/**
 * 解析 multipart body。
 * 参数 body：完整请求体；boundary：分隔串（不含前导 --）。
 */
export function parse_multipart(body: Buffer, boundary: string): multipart_result {
  const result: multipart_result = { fields: {}, files: [] };
  const delimiter = Buffer.from(`--${boundary}`, 'utf8');
  const next_delimiter = Buffer.from(`\r\n--${boundary}`, 'utf8');

  // 第一段之前是 preamble，直接跳过
  let start = body.indexOf(delimiter);
  if (start < 0) return result;
  start += delimiter.length;

  while (start < body.length) {
    // 结束标记：--boundary--
    if (body[start] === 0x2d && body[start + 1] === 0x2d) break;
    // 跳过分隔符后的 CRLF
    if (body[start] === 0x0d && body[start + 1] === 0x0a) start += 2;

    const head_end = body.indexOf('\r\n\r\n', start);
    if (head_end < 0) break;
    const head_text = body.subarray(start, head_end).toString('utf8');
    const data_start = head_end + 4;

    const next_cut = body.indexOf(next_delimiter, data_start);
    const data_end = next_cut < 0 ? body.length : next_cut;
    const data = body.subarray(data_start, data_end);

    const headers: Record<string, string> = {};
    for (const line of head_text.split('\r\n')) {
      const idx = line.indexOf(':');
      if (idx > 0) headers[line.slice(0, idx).trim().toLowerCase()] = line.slice(idx + 1).trim();
    }
    const disposition = headers['content-disposition'] || '';
    const field = read_disposition(disposition, 'name') || '';
    const filename = read_disposition(disposition, 'filename');

    if (filename !== null) {
      result.files.push({
        field,
        filename,
        contentType: headers['content-type'] || 'application/octet-stream',
        data: Buffer.from(data),
      });
    } else if (field) {
      result.fields[field] = data.toString('utf8');
    }

    if (next_cut < 0) break;
    start = next_cut + next_delimiter.length;
  }
  return result;
}

/**
 * 从 Content-Disposition 里取某个参数值。
 * 参数 disposition：头内容；key：参数名（name / filename）。
 * 返回 null 表示该参数不存在（用来区分「没有 filename」和「filename 为空」）。
 */
function read_disposition(disposition: string, key: string): string | null {
  const pattern = new RegExp(`${key}=(?:"([^"]*)"|([^;]+))`, 'i');
  const match = pattern.exec(disposition);
  if (!match) return null;
  return (match[1] !== undefined ? match[1] : match[2] || '').trim();
}
