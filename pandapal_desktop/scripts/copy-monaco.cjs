const fs = require('fs');
const path = require('path');

const src = path.join(__dirname, '..', 'node_modules', 'monaco-editor', 'min', 'vs');
const dest = path.join(__dirname, '..', 'public', 'monaco-editor', 'min', 'vs');

fs.mkdirSync(path.dirname(dest), { recursive: true });
fs.cpSync(src, dest, { recursive: true });
console.log('monaco copied');
