export function normalize(value) {
  return value.trim().toLowerCase();
}

export function wordCount(value) {
  return value.trim().split(/\s+/).filter(Boolean).length;
}
