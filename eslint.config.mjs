export default [{
  files: ["token_tracker/static/*.js"],
  languageOptions: { ecmaVersion: "latest", sourceType: "script" },
  rules: {
    "no-unreachable": "error",
    "no-dupe-keys": "error",
    "no-duplicate-case": "error",
    "no-self-assign": "error",
    "no-func-assign": "error",
    "no-dupe-else-if": "error",
    "no-redeclare": "error",
    "no-constant-condition": ["error", { checkLoops: false }],
  },
}];
