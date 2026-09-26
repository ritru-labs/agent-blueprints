export function requireApiKey(value) {
  if (typeof value !== "string" || !value.trim()) {
    throw new Error("OPENAI_API_KEY is missing. Set it locally before explicitly running npm run spike.");
  }
  return value;
}
