#!/usr/bin/env node
/**
 * Wrapper for @tacticlaunch/mcp-linear.
 *
 * That package writes debug output (console.log) to stdout, which corrupts
 * the MCP JSON-RPC stream. This wrapper spawns the real server and silently
 * redirects any non-JSON lines from its stdout to stderr so the protocol
 * layer only ever sees valid JSON-RPC messages.
 */
'use strict';

const { spawn } = require('child_process');
const readline = require('readline');

const server = spawn('npx', ['-y', '@tacticlaunch/mcp-linear'], {
  env: process.env,
  stdio: ['pipe', 'pipe', 'inherit'],  // pipe stdin/stdout; inherit stderr
});

// Forward our stdin to the inner server's stdin.
process.stdin.pipe(server.stdin);

// Read the server's stdout line by line.
// Only forward lines that are valid JSON — everything else is debug noise.
const rl = readline.createInterface({ input: server.stdout, terminal: false });
rl.on('line', (line) => {
  const trimmed = line.trim();
  if (!trimmed) return;
  try {
    const parsed = JSON.parse(trimmed);
    // JSON-RPC messages are always objects. Bare strings/numbers/arrays are
    // debug noise from the server leaking onto stdout.
    if (parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)) {
      process.stdout.write(trimmed + '\n');
    } else {
      process.stderr.write('[linear-mcp] ' + trimmed + '\n');
    }
  } catch {
    process.stderr.write('[linear-mcp] ' + trimmed + '\n');
  }
});

server.on('error', (err) => {
  process.stderr.write('linear-mcp-wrapper: ' + err.message + '\n');
  process.exit(1);
});

server.on('exit', (code) => process.exit(code ?? 0));
