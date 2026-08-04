/**
 * NDJSON stream reader.
 *
 * /chat/stream emits one JSON object per line. Chunk boundaries do not respect
 * line boundaries, so we buffer and split on newlines rather than parsing each
 * chunk directly.
 */

/**
 * Yield each parsed JSON object from an NDJSON response body.
 *
 * @param {Response} response
 * @returns {AsyncGenerator<object>}
 */
export async function* readNDJSON(response) {
  if (!response.body) {
    throw new Error('Response has no body to stream');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      let newline;
      while ((newline = buffer.indexOf('\n')) !== -1) {
        const line = buffer.slice(0, newline).trim();
        buffer = buffer.slice(newline + 1);
        if (line) yield parseLine(line);
      }
    }

    // Flush the decoder, then whatever is left without a trailing newline.
    buffer += decoder.decode();
    const tail = buffer.trim();
    if (tail) yield parseLine(tail);
  } finally {
    reader.releaseLock();
  }
}

/**
 * Parse one NDJSON line. A malformed line surfaces as a system message rather
 * than killing the stream — the rest of the turn is still worth showing.
 */
function parseLine(line) {
  try {
    return JSON.parse(line);
  } catch {
    console.error('Malformed NDJSON line:', line);
    return { role: 'system', content: 'Received a malformed message from the server.' };
  }
}
