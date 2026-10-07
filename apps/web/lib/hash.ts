// 006-web T032 (research R6, F-01.R2): SHA-256 of a file in the browser with Web Crypto,
// lower-case hex, the value EP-06 declares and EP-07 checks the stored object against.
// Files are at most 10 MB, so hashing the whole buffer at once is fine.

export async function sha256Hex(file: Blob): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}
