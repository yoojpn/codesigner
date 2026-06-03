import React, { useState, useEffect, useRef, useCallback } from 'react'
import { ChevronRight, ChevronDown, File, Folder, FolderOpen, X, Check, Terminal,
  Search, RefreshCw, Download, Plus, Trash2, Send, Loader2, AlertTriangle,
  Globe, Code2, Cpu, Paperclip, MessageSquare, Edit2, Copy, Lock, FilePen, FileText } from 'lucide-react'
import MonacoEditor from '@monaco-editor/react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import './App.css'

// Markdown用カスタムコンポーネント（コードブロックなど）
const MarkdownComponents = {
  code({ node, inline, className, children, ...props }) {
    const match = /language-(\w+)/.exec(className || '')
    if (!inline) {
      return (
        <div className="md-code-block">
          {match && <div className="md-code-lang">{match[1]}</div>}
          <pre className="md-pre"><code className={className} {...props}>{children}</code></pre>
        </div>
      )
    }
    return <code className="md-inline-code" {...props}>{children}</code>
  },
  table({ children }) { return <div className="md-table-wrap"><table className="md-table">{children}</table></div> },
  a({ href, children }) { return <a href={href} target="_blank" rel="noreferrer">{children}</a> },
}

function Markdown({ children, streaming = false }) {
  return (
    <div className={`markdown-body${streaming ? ' streaming' : ''}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={MarkdownComponents}>{children || ''}</ReactMarkdown>
    </div>
  )
}

// ---- Backend URL ----
const BACKEND_URL = (typeof __BACKEND_URL__ !== 'undefined' && __BACKEND_URL__) ? __BACKEND_URL__.replace(/\/$/, '') : ''
const apiUrl = (path) => `${BACKEND_URL}${path}`

// ---- Login Screen ----
function LoginScreen({ onLogin }) {
  const [passcode, setPasscode] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  async function handleSubmit() {
    if (!passcode.trim()) return
    setLoading(true)
    setError('')
    try {
      const r = await fetch(apiUrl('/api/auth'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ passcode })
      })
      if (r.ok) {
        sessionStorage.setItem('codesigner_auth', passcode)
        onLogin()
      } else {
        setError('パスコードが違います')
      }
    } catch {
      setError('接続エラー')
    }
    setLoading(false)
  }

  return (
    <div className="login-screen">
      <div className="login-card">
        <div className="login-logo">
          <span className="logo-icon">⌘</span>
          <span className="logo-text">codesigner</span>
        </div>
        <div className="login-icon"><Lock size={24} /></div>
        <h2 className="login-title">パスコードを入力</h2>
        <input
          className="login-input"
          type="password"
          placeholder="Passcode"
          value={passcode}
          autoFocus
          onChange={e => setPasscode(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && handleSubmit()}
        />
        {error && <div className="login-error">{error}</div>}
        <button className="login-btn" onClick={handleSubmit} disabled={loading || !passcode.trim()}>
          {loading ? <Loader2 size={14} className="spin" /> : 'ログイン'}
        </button>
      </div>
    </div>
  )
}

// ---- WebSocket Hook ----
function useAgent(chatId) {
  const ws = useRef(null)
  const [connected, setConnected] = useState(false)
  const handlers = useRef({})
  const chatIdRef = useRef(chatId)

  useEffect(() => { chatIdRef.current = chatId }, [chatId])

  const connect = useCallback(() => {
    if (!chatId) return
    // 既存WSを閉じる
    if (ws.current) {
      ws.current.onclose = null // 自動再接続を無効化
      ws.current.close()
      ws.current = null
    }
    setConnected(false)
    const _backendUrl = BACKEND_URL
    const wsProto = _backendUrl ? (_backendUrl.startsWith('https') ? 'wss' : 'ws') : (location.protocol === 'https:' ? 'wss' : 'ws')
    const wsHost = _backendUrl ? _backendUrl.replace(/^https?:\/\//, '') : location.host
    const sock = new WebSocket(`${wsProto}://${wsHost}/ws/${chatId}`)
    sock.onopen = () => setConnected(true)
    sock.onclose = () => {
      setConnected(false)
      // 同じchatIdなら再接続
      if (chatIdRef.current === chatId) {
        setTimeout(() => { if (chatIdRef.current === chatId) connect() }, 3000)
      }
    }
    sock.onerror = () => sock.close()
    sock.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data)
        // 現在のchatIdのメッセージのみ処理
        handlers.current[msg.type]?.(msg)
      } catch {}
    }
    ws.current = sock
  }, [chatId])

  useEffect(() => {
    connect()
    return () => {
      if (ws.current) {
        ws.current.onclose = null
        ws.current.close()
        ws.current = null
      }
    }
  }, [connect])

  const send = useCallback((data) => {
    if (ws.current?.readyState === WebSocket.OPEN) ws.current.send(JSON.stringify(data))
  }, [])
  const on = useCallback((type, fn) => { handlers.current[type] = fn }, [])

  return { connected, send, on }
}

// ---- File Tree ----
function getFileIcon(name, isDir, isOpen) {
  if (isDir) return isOpen
    ? <FolderOpen size={14} className="file-icon folder-open" />
    : <Folder size={14} className="file-icon folder" />
  const ext = name.split('.').pop().toLowerCase()
  return <File size={14} className={`file-icon ext-${['js','jsx','ts','tsx','py','json','md','css','html','sh'].includes(ext) ? ext : 'default'}`} />
}

function FileTree({ files, selected, onSelect, onRefresh, onDownload }) {
  const [expanded, setExpanded] = useState(new Set(['.']))

  function buildTree(flatList) {
    const root = {}
    for (const f of flatList) {
      const parts = f.path.split('/')
      let node = root
      for (let i = 0; i < parts.length; i++) {
        const part = parts[i]
        if (!node[part]) node[part] = { __meta: { ...f, name: part, isDir: i < parts.length - 1 || f.type === 'dir' } }
        node = node[part]
      }
    }
    return root
  }

  const tree = buildTree(files)

  function toggle(path) {
    setExpanded(s => { const n = new Set(s); n.has(path) ? n.delete(path) : n.add(path); return n })
  }

  function renderNode(node, path = '', depth = 0) {
    return Object.entries(node)
      .filter(([k]) => k !== '__meta')
      .sort(([, av], [, bv]) => {
        const aDir = av.__meta?.isDir; const bDir = bv.__meta?.isDir
        if (aDir && !bDir) return -1; if (!aDir && bDir) return 1; return 0
      })
      .map(([key, val]) => {
        const meta = val.__meta || { name: key, isDir: false }
        const fullPath = path ? `${path}/${key}` : key
        const isDir = meta.isDir || Object.keys(val).filter(k => k !== '__meta').length > 0
        const isExp = expanded.has(fullPath)
        const isSelected = selected === fullPath
        return (
          <div key={fullPath}>
            <div className={`tree-item ${isSelected ? 'selected' : ''}`} style={{ paddingLeft: `${depth * 12 + 8}px` }}
              onClick={() => { isDir ? toggle(fullPath) : onSelect(fullPath) }}>
              <span className="tree-icon">{isDir ? (isExp ? <ChevronDown size={12} /> : <ChevronRight size={12} />) : null}</span>
              {getFileIcon(key, isDir, isExp)}
              <span className="tree-label">{key}</span>
              {!isDir && (
                <span className="tree-item-actions">
                  <button className="icon-btn" title="Download" onClick={(e) => { e.stopPropagation(); onDownload(fullPath) }}><Download size={11} /></button>
                </span>
              )}
            </div>
            {isDir && isExp && renderNode(val, fullPath, depth + 1)}
          </div>
        )
      })
  }

  return (
    <div className="file-tree">
      <div className="panel-header">
        <span>Files</span>
        <button className="icon-btn" title="Refresh" onClick={onRefresh}><RefreshCw size={12} /></button>
      </div>
      <div className="tree-body">
        {files.length === 0
          ? <div className="empty-state">No files yet</div>
          : renderNode(tree)}
      </div>
    </div>
  )
}

// ---- Chat Sidebar ----
function ChatSidebar({ chats, activeChatId, onSelect, onCreate, onDelete, onRename }) {
  const [editingId, setEditingId] = useState(null)
  const [editValue, setEditValue] = useState('')

  function startEdit(e, chat) {
    e.stopPropagation()
    setEditingId(chat.id)
    setEditValue(chat.title)
  }

  function commitEdit(id) {
    if (editValue.trim()) onRename(id, editValue.trim())
    setEditingId(null)
  }

  function formatDate(iso) {
    const d = new Date(iso + 'Z')
    const now = new Date()
    const diff = now - d
    if (diff < 60000) return 'just now'
    if (diff < 3600000) return `${Math.floor(diff/60000)}m ago`
    if (diff < 86400000) return `${Math.floor(diff/3600000)}h ago`
    return d.toLocaleDateString()
  }

  return (
    <div className="chat-sidebar">
      <div className="chat-sidebar-header">
        <span className="logo-row">
          <span className="logo-icon">⌘</span>
          <span className="logo-text">codesigner</span>
        </span>
        <button className="new-chat-btn" onClick={onCreate} title="New Chat"><Plus size={14} /></button>
      </div>
      <div className="chat-list">
        {chats.length === 0 && <div className="empty-state" style={{padding:'16px',textAlign:'center'}}>No chats yet</div>}
        {chats.map(chat => (
          <div key={chat.id} className={`chat-item ${chat.id === activeChatId ? 'active' : ''}`} onClick={() => onSelect(chat.id)}>
            <MessageSquare size={13} className="chat-item-icon" />
            <div className="chat-item-body">
              {editingId === chat.id ? (
                <input
                  className="chat-rename-input"
                  value={editValue}
                  autoFocus
                  onChange={e => setEditValue(e.target.value)}
                  onBlur={() => commitEdit(chat.id)}
                  onKeyDown={e => { if (e.key === 'Enter') commitEdit(chat.id); if (e.key === 'Escape') setEditingId(null) }}
                  onClick={e => e.stopPropagation()}
                />
              ) : (
                <span className="chat-item-title">{chat.title}</span>
              )}
              <span className="chat-item-date">{formatDate(chat.updated_at)}</span>
            </div>
            <div className="chat-item-actions">
              <button className="icon-btn" title="Rename" onClick={(e) => startEdit(e, chat)}><Edit2 size={11} /></button>
              <button className="icon-btn danger" title="Delete" onClick={(e) => { e.stopPropagation(); onDelete(chat.id) }}><Trash2 size={11} /></button>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

// ---- Output Panel ----
function OutputPanel({ outputs, onRefresh, onDownload, onDelete }) {
  function formatSize(b) {
    if (b < 1024) return `${b}B`
    if (b < 1048576) return `${(b/1024).toFixed(1)}KB`
    return `${(b/1048576).toFixed(1)}MB`
  }
  return (
    <div className="output-panel">
      <div className="panel-header">
        <span>Downloads</span>
        <button className="icon-btn" title="Refresh" onClick={onRefresh}><RefreshCw size={12} /></button>
      </div>
      <div className="output-list">
        {outputs.length === 0
          ? <div className="empty-outputs">No output files</div>
          : outputs.map(f => (
            <div className="output-item" key={f.name}>
              <span className="output-name" title={f.name}>{f.name}</span>
              <span className="output-size">{formatSize(f.size)}</span>
              <div className="output-actions">
                <button className="download-btn" onClick={() => onDownload(f.path)}><Download size={11} /> DL</button>
                <button className="delete-output-btn" onClick={() => onDelete(f.name)}><X size={11} /></button>
              </div>
            </div>
          ))
        }
      </div>
    </div>
  )
}

// ---- Tool Views ----
function DiffView({ diff }) {
  const lines = diff.split('\n')
  const added = lines.filter(l => l.startsWith('+')).length
  const removed = lines.filter(l => l.startsWith('-')).length
  const total = added + removed || 1
  const addSegs = Math.round((added / total) * 10)
  return (
    <div className="diff-wrap">
      <div className="diff-stats">
        <span className="diff-stat-add">+{added}</span>
        <span className="diff-stat-del">−{removed}</span>
        <span className="diff-stat-bar">
          {Array.from({ length: 10 }).map((_, i) => (
            <span key={i} className={`diff-stat-seg ${i < addSegs ? 'add' : 'del'}`} />
          ))}
        </span>
      </div>
      <div className="diff-view">
        {lines.map((line, i) => {
          const cls = line.startsWith('+') ? 'add' : line.startsWith('-') ? 'del' : line.startsWith('@@') ? 'hunk' : 'ctx'
          return <div key={i} className={`diff-line ${cls}`}>{line || ' '}</div>
        })}
      </div>
    </div>
  )
}

// ツールのグループ表示ラベル
function toolGroupLabel(items) {
  const fileEdits = items.filter(t => ['apply_diff','write_file'].includes(t.tool))
  const cmds = items.filter(t => t.tool === 'run_command')
  const reads = items.filter(t => t.tool === 'read_file')
  const searches = items.filter(t => ['web_search','search_files','fetch_url'].includes(t.tool))

  const parts = []
  if (fileEdits.length === 1) {
    parts.push(`${fileEdits[0].tool === 'write_file' ? 'ファイルを作成' : 'ファイルを編集'} · ${fileEdits[0].args?.path?.split('/').pop() || ''}`)
  } else if (fileEdits.length > 1) {
    parts.push(`${fileEdits.length}件のファイルを編集`)
  }
  if (cmds.length === 1) parts.push(`実行 · ${(cmds[0].args?.command || '').slice(0, 40)}`)
  else if (cmds.length > 1) parts.push(`${cmds.length}件のコマンドを実行`)
  if (reads.length === 1) parts.push(`読み込み · ${reads[0].args?.path?.split('/').pop() || ''}`)
  else if (reads.length > 1) parts.push(`${reads.length}件のファイルを読み込み`)
  if (searches.length > 0) parts.push(`検索`)
  if (parts.length === 0) {
    const names = [...new Set(items.map(t => t.tool))].join(', ')
    parts.push(names)
  }
  return parts.join(' · ')
}

function toolGroupIcon(items) {
  const tools = items.map(t => t.tool)
  if (tools.some(t => ['apply_diff','write_file'].includes(t))) return <FilePen size={12} />
  if (tools.some(t => t === 'run_command')) return <Terminal size={12} />
  if (tools.some(t => t === 'read_file')) return <FileText size={12} />
  if (tools.some(t => ['web_search','fetch_url'].includes(t))) return <Globe size={12} />
  return <Code2 size={12} />
}

function DiffResultView({ diffResult }) {
  const [expanded, setExpanded] = useState(false)
  if (!diffResult) return null
  const { path, added = 0, removed = 0, diff = '' } = diffResult
  const filename = path?.split('/').pop() || path

  // diff テキストをパース（hunks単位）
  const hunks = []
  let currentHunk = null
  for (const raw of diff.split('\n')) {
    if (raw.startsWith('--- ') || raw.startsWith('+++ ')) continue
    if (raw.startsWith('@@')) {
      if (currentHunk) hunks.push(currentHunk)
      currentHunk = { header: raw, lines: [] }
    } else if (currentHunk) {
      if (raw.startsWith('+')) currentHunk.lines.push({ type: 'add', text: raw.slice(1) })
      else if (raw.startsWith('-')) currentHunk.lines.push({ type: 'del', text: raw.slice(1) })
      else if (raw !== '\\ No newline at end of file') currentHunk.lines.push({ type: 'ctx', text: raw.slice(1) })
    }
  }
  if (currentHunk) hunks.push(currentHunk)

  const totalChanges = added + removed
  const addPct = totalChanges > 0 ? Math.round((added / totalChanges) * 5) : 0

  return (
    <div className="diff-card">
      <button className="diff-card-header" onClick={() => setExpanded(v => !v)}>
        <span className="diff-card-icon"><Check size={12} /></span>
        <span className="diff-card-filename">{filename}</span>
        <span className="diff-card-path">{path}</span>
        <span className="diff-card-add">+{added}</span>
        <span className="diff-card-del">−{removed}</span>
        <span className="diff-card-segments">
          {Array.from({length: 5}).map((_, i) => (
            <span key={i} className={`diff-seg ${i < addPct ? 'add' : removed > 0 ? 'del' : 'neutral'}`} />
          ))}
        </span>
        <span className="diff-card-chevron">{expanded ? '▲' : '▼'}</span>
      </button>
      {expanded && (
        <div className="diff-body">
          {hunks.length === 0 && <div className="diff-empty">変更なし</div>}
          {hunks.map((hunk, hi) => (
            <div key={hi} className="diff-hunk-block">
              <div className="diff-hunk-header">{hunk.header}</div>
              {hunk.lines.map((line, li) => (
                <div key={li} className={`diff-row diff-row-${line.type}`}>
                  <span className="diff-gutter">{line.type === 'add' ? '+' : line.type === 'del' ? '−' : ' '}</span>
                  <span className="diff-code">{line.text}</span>
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function ToolGroup({ group }) {
  const [expanded, setExpanded] = useState(false)
  const items = group.items || []
  const isDone = items.every(t => t.result !== undefined || t.skipped)
  const hasError = items.some(t => t.result?.error && t.result?.error !== 'already_applied')
  const label = toolGroupLabel(items)
  const icon = toolGroupIcon(items)
  return (
    <div className={`tool-group ${isDone ? 'done' : 'running'} ${hasError ? 'has-error' : ''}`}>
      <button className="tool-group-header" onClick={() => setExpanded(v => !v)}>
        <span className="tool-group-icon">{icon}</span>
        <span className="tool-group-label">{label}</span>
        {!isDone && <Loader2 size={11} className="spin tool-group-spinner" />}
        {isDone && !hasError && <Check size={11} className="tool-group-check" />}
        {hasError && <AlertTriangle size={11} className="tool-group-error" />}
        <span className="tool-group-chevron">{expanded ? '▲' : '▼'}</span>
      </button>
      {expanded && (
        <div className="tool-group-details">
          {items.map((t, i) => (
            <div key={i} className="tool-detail-row">
              <span className="tool-detail-name">{t.tool}</span>
              {t.tool === 'run_command' && <span className="tool-detail-args">$ {t.args?.command}</span>}
              {t.tool === 'web_search' && <span className="tool-detail-args">"{t.args?.query}"</span>}
              {['read_file','write_file','apply_diff'].includes(t.tool) && <span className="tool-detail-args">{t.args?.path}</span>}
              {t.result && (
                <span className={`tool-detail-result ${t.result.error && t.result.error !== 'already_applied' ? 'error' : 'ok'}`}>
                  {t.result.error && t.result.error !== 'already_applied' ? `✕ ${t.result.error}` : '✓'}
                </span>
              )}
            </div>
          ))}
          {/* cmdのstdout表示 */}
          {items.filter(t => t.tool === 'run_command' && t.result?.stdout).map((t, i) => (
            <pre key={i} className="cmd-output">{(t.result.stdout + (t.result.stderr || '')).slice(0, 800)}</pre>
          ))}
          {/* 検索結果 */}
          {items.filter(t => t.tool === 'web_search' && t.result?.results).flatMap((t, i) =>
            (t.result.results || []).slice(0, 3).map((r, j) => (
              <div key={`${i}-${j}`} className="search-result">
                <div className="search-result-url">{r.url}</div>
                <div className="search-result-snippet">{r.snippet}</div>
              </div>
            ))
          )}
        </div>
      )}
    </div>
  )
}

function ToolCallBadge({ tool, args }) {
  const [diffExpanded, setDiffExpanded] = useState(false)
  const icons = { web_search: <Globe size={13} />, run_command: <Terminal size={13} />, apply_diff: <Code2 size={13} />, search_files: <Search size={13} />, fetch_url: <Globe size={13} />, copy_to_output: <Download size={13} /> }
  const icon = icons[tool] || <Code2 size={13} />
  let argsDisplay = ''
  if (tool === 'web_search') argsDisplay = `"${args.query}"`
  else if (tool === 'run_command') argsDisplay = `$ ${args.command}`
  else if (tool === 'fetch_url') argsDisplay = args.url
  else if (tool === 'apply_diff') {
    const diffLines = (args.diff || '').split('\n')
    const added = diffLines.filter(l => l.startsWith('+') && !l.startsWith('+++')).length
    const removed = diffLines.filter(l => l.startsWith('-') && !l.startsWith('---')).length
    argsDisplay = (
      <span style={{display:'flex', alignItems:'center', gap:'6px', flexWrap:'wrap'}}>
        <span>{args.path}</span>
        {(added > 0 || removed > 0) && (
          <button
            className="diff-toggle-btn"
            onClick={e => { e.stopPropagation(); setDiffExpanded(v => !v) }}
            title="diff内容を表示/非表示"
          >
            <span className="diff-stat-add">+{added}</span>
            <span className="diff-stat-del"> −{removed}</span>
            <span className="diff-toggle-chevron">{diffExpanded ? '▲' : '▼'}</span>
          </button>
        )}
      </span>
    )
  }
  else if (tool === 'copy_to_output') argsDisplay = `${args.path} → output/${args.output_name || args.path?.split('/').pop()}`
  else argsDisplay = JSON.stringify(args, null, 2)
  return (
    <div className="tool-badge">
      <span className="tool-badge-icon">{icon}</span>
      <div className="tool-badge-body">
        <div className="tool-badge-header"><span className="tool-name">{tool}</span></div>
        <div className="tool-args">{argsDisplay}</div>
        {tool === 'apply_diff' && args.diff && diffExpanded && <DiffView diff={args.diff} />}
      </div>
    </div>
  )
}

function ToolResultView({ tool, result }) {
  if (!result) return null
  if (result.cancelled) return <div className="tool-result neutral"><X size={12} /> Cancelled</div>
  if (result.error) return <div className="tool-result error"><AlertTriangle size={12} /> {result.error}</div>
  if (tool === 'run_command') {
    return (
      <div className="tool-result neutral">
        <pre className="cmd-output">{result.stdout || result.stderr || '(no output)'}</pre>
        <span className={`exit-code ${result.exit_code === 0 ? 'ok' : 'fail'}`}>exit {result.exit_code}</span>
      </div>
    )
  }
  if (tool === 'web_search' && result.results) {
    return (
      <div className="search-results">
        {result.results.map((r, i) => (
          <div key={i} className="search-result">
            <div className="search-result-url">{r.url}</div>
            <div className="search-result-snippet">{r.snippet}</div>
          </div>
        ))}
      </div>
    )
  }
  if (tool === 'read_file' && result.content) {
    return <div className="tool-result neutral"><pre className="cmd-output">{result.content.slice(0, 500)}{result.content.length > 500 ? '\n…' : ''}</pre></div>
  }
  if (tool === 'apply_diff' && result.success) {
    const filename = result.path?.split('/').pop() || result.path || ''
    const added = result.lines_changed ?? 0
    const removed = result.lines_removed ?? 0
    return (
      <div className="tool-result success diff-result">
        <span className="diff-result-icon"><Check size={12} /></span>
        <span className="diff-result-file">{filename}</span>
        <span className="diff-result-stats">
          {added > 0 && <span className="diff-stat-add">+{added}</span>}
          {removed > 0 && <span className="diff-stat-del"> −{removed}</span>}
        </span>
      </div>
    )
  }
  if (tool === 'write_file' && result.success) {
    const filename = result.path?.split('/').pop() || result.path || ''
    return (
      <div className="tool-result success diff-result">
        <span className="diff-result-icon"><Check size={12} /></span>
        <span className="diff-result-file">{filename}</span>
        <span className="diff-result-badge">written</span>
      </div>
    )
  }
  if (tool === 'copy_to_output' && result.success) {
    const filename = result.output_path?.split('/').pop() || result.output_path || ''
    return (
      <div className="tool-result success diff-result">
        <span className="diff-result-icon"><Check size={12} /></span>
        <span className="diff-result-file">{filename}</span>
        <span className="diff-result-badge">output保存済み</span>
      </div>
    )
  }
  return <div className="tool-result neutral"><pre className="cmd-output">{JSON.stringify(result, null, 2).slice(0, 300)}</pre></div>
}

function ApprovalCard({ msg, onApprove, onReject }) {
  return (
    <div className="approval-card">
      <div className="approval-header"><AlertTriangle size={14} /> Approval required</div>
      <div className="approval-tool">{msg.tool}</div>
      <div className="approval-reason">{msg.reason}</div>
      {msg.args?.command && <pre className="approval-command">$ {msg.args.command}</pre>}
      {msg.args?.path && <div className="approval-path">Path: {msg.args.path}</div>}
      <div className="approval-actions">
        <button className="approve-btn" onClick={onApprove}><Check size={13} /> Allow</button>
        <button className="reject-btn" onClick={onReject}><X size={13} /> Deny</button>
      </div>
    </div>
  )
}

// ---- Editor Area with HTML Preview ----
function EditorArea({ selectedFile, fileContent, lang }) {
  const [viewMode, setViewMode] = useState('code') // 'code' | 'preview'
  const isHtml = selectedFile?.toLowerCase().endsWith('.html')

  // ファイルが変わったらcodeに戻す
  useEffect(() => { setViewMode('code') }, [selectedFile])

  if (!selectedFile) return <div className="editor-area" />

  return (
    <div className="editor-area">
      {isHtml && (
        <div className="editor-toolbar">
          <button
            className={`editor-view-btn ${viewMode === 'code' ? 'active' : ''}`}
            onClick={() => setViewMode('code')}
          >
            <Code2 size={12} /> コード
          </button>
          <button
            className={`editor-view-btn ${viewMode === 'preview' ? 'active' : ''}`}
            onClick={() => setViewMode('preview')}
          >
            <Globe size={12} /> プレビュー
          </button>
          <span className="editor-toolbar-filename">{selectedFile.split('/').pop()}</span>
        </div>
      )}
      {viewMode === 'preview' && isHtml ? (
        <iframe
          className="html-preview"
          srcDoc={fileContent}
          sandbox="allow-scripts allow-same-origin"
          title="HTML Preview"
        />
      ) : (
        <MonacoEditor
          height="100%"
          language={lang}
          value={fileContent}
          theme="vs-dark"
          options={{ readOnly: true, minimap: { enabled: false }, fontSize: 13, scrollBeyondLastLine: false, lineNumbers: 'on' }}
        />
      )}
    </div>
  )
}

// ---- Message Renderer ----
function MessageList({ messages, onRetry, onEditSend, activeChatId, diffResults = {} }) {
  const bottomRef = useRef(null)
  const [editingIdx, setEditingIdx] = useState(null)
  const [editValue, setEditValue] = useState('')
  const [copiedIdx, setCopiedIdx] = useState(null)

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages, diffResults])

  function copyText(text, idx) {
    navigator.clipboard.writeText(text).then(() => {
      setCopiedIdx(idx)
      setTimeout(() => setCopiedIdx(null), 1500)
    }).catch(() => {})
  }

  function startEdit(i, content) {
    setEditingIdx(i)
    setEditValue(content)
  }

  function commitEdit(i) {
    const msg = messages[i]
    if (editValue.trim()) onEditSend(editValue.trim(), msg?.dbIndex ?? null)
    setEditingIdx(null)
  }

  return (
    <div className="messages">
      {messages.map((msg, i) => {
        if (msg.type === 'user') return (
          <div key={i} className="message user-msg">
            <div className="user-msg-wrap">
              {editingIdx === i ? (
                <div className="user-edit-wrap">
                  <textarea
                    className="user-edit-input"
                    value={editValue}
                    autoFocus
                    onChange={e => setEditValue(e.target.value)}
                    onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); commitEdit(i) } if (e.key === 'Escape') setEditingIdx(null) }}
                  />
                  <div className="user-edit-actions">
                    <button className="edit-save-btn" onClick={() => commitEdit(i)}><Check size={11} /> 送信</button>
                    <button className="edit-cancel-btn" onClick={() => setEditingIdx(null)}><X size={11} /> キャンセル</button>
                  </div>
                </div>
              ) : (
                <div className="msg-content">{msg.content}</div>
              )}
              <div className="user-msg-actions">
                <button className="msg-action-btn" title="編集" onClick={() => startEdit(i, msg.content)}><Edit2 size={12} /></button>
                <button className={`msg-action-btn ${copiedIdx === i ? 'copied' : ''}`} title="コピー" onClick={() => copyText(msg.content, i)}>
                  {copiedIdx === i ? <Check size={12} /> : <Copy size={12} />}
                </button>
              </div>
            </div>
          </div>
        )
        if (msg.type === 'text' || msg.type === 'streaming') return (
          <div key={i} className="message agent-msg">
            <div className="msg-avatar"><Cpu size={12} /></div>
            <div className="msg-body">
              <div className="msg-content">
                <Markdown streaming={msg.type === 'streaming'}>{msg.content}</Markdown>
              </div>
              <div className="agent-msg-actions">
                <button className={`msg-action-btn ${copiedIdx === i ? 'copied' : ''}`} title="コピー" onClick={() => copyText(msg.content, i)}>
                  {copiedIdx === i ? <Check size={12} /> : <Copy size={12} />}
                </button>
              </div>
            </div>
          </div>
        )
        if (msg.type === 'error') return (
          <div key={i} className="message agent-msg">
            <div className="msg-avatar"><AlertTriangle size={12} /></div>
            <div className="msg-body">
              <div className="msg-content error-msg">Error: {msg.content}</div>
              <div className="agent-msg-actions">
                <button className="retry-btn" onClick={() => { const lastUser = [...messages].reverse().find(m => m.type === "user"); onRetry(lastUser?.dbIndex ?? null) }}><RefreshCw size={11} /> 再試行</button>
              </div>
            </div>
          </div>
        )
        if (msg.type === 'system') return (
          <div key={i} className="message system-msg">
            <Markdown>{msg.content}</Markdown>
          </div>
        )
        if (msg.type === 'tool_group') return <ToolGroup key={i} group={msg} />
        if (msg.type === 'tool_call') return <ToolCallBadge key={i} tool={msg.tool} args={msg.args} />
        if (msg.type === 'tool_result') return <ToolResultView key={i} tool={msg.tool} result={msg.result} />
        if (msg.type === 'diff_summary') return (
          <div key={i} className="diff-summary-block">
            {Object.entries(msg.files).map(([path, info]) => {
              const filename = path.split('/').pop()
              const total = (info.added + info.removed) || 1
              const addSegs = Math.round((info.added / total) * 8)
              const isPending = info.status === 'pending'
              return (
                <div key={path} className={`diff-summary-row ${isPending ? 'pending' : ''}`}>
                  {isPending
                    ? <Loader2 size={11} className="diff-summary-spinner spin" />
                    : <Check size={11} className="diff-summary-check" />}
                  <span className="diff-summary-file">{filename}</span>
                  <span className="diff-stat-add">+{info.added}</span>
                  <span className="diff-stat-del"> −{info.removed}</span>
                  {!isPending && (
                    <span className="diff-stat-bar" style={{marginLeft:4}}>
                      {Array.from({ length: 8 }).map((_, si) => (
                        <span key={si} className={`diff-stat-seg ${si < addSegs ? 'add' : 'del'}`} />
                      ))}
                    </span>
                  )}
                </div>
              )
            })}
          </div>
        )
        if (msg.type === 'output_files') return (
          <div key={i} className="message agent-msg">
            <div className="msg-avatar"><Cpu size={12} /></div>
            <div className="msg-body">
              <div className="output-file-cards">
                {msg.files.map((f, fi) => (
                  <a
                    key={fi}
                    className="output-file-card"
                    href={apiUrl(`/api/download?path=${encodeURIComponent(f.path)}&chat_id=${encodeURIComponent(activeChatId)}`)}
                    download={f.name}
                    target="_blank"
                    rel="noreferrer"
                  >
                    <Download size={14} className="output-file-card-icon" />
                    <span className="output-file-card-name">{f.name}</span>
                  </a>
                ))}
              </div>
            </div>
          </div>
        )
        if (msg.type === 'cmd_streaming') return (
          <div key={i} className="tool-badge">
            <span className="tool-badge-icon"><Terminal size={13} /></span>
            <div className="tool-badge-body">
              <div className="tool-badge-header"><span className="tool-name">run_command</span> <Loader2 size={11} className="spin" /></div>
              <pre className="cmd-output cmd-live">
                {msg.lines.slice(-40).join('\n')}
              </pre>
            </div>
          </div>
        )
        if (msg.type === 'thinking') return (
          <div key={i} className="message agent-msg">
            <div className="msg-avatar"><Cpu size={12} /></div>
            <div className="thinking"><Loader2 size={13} className="spin" /> thinking…</div>
          </div>
        )
        return null
      })}
      {Object.keys(diffResults).length > 0 && (
        <div className="diff-panel">
          {Object.entries(diffResults).map(([path, d]) => (
            <DiffResultView key={path} diffResult={d} />
          ))}
        </div>
      )}
      <div ref={bottomRef} />
    </div>
  )
}

// ---- Main App ----
export default function App() {
  const [authChecked, setAuthChecked] = useState(false)
  const [authed, setAuthed] = useState(false)
  const [authRequired, setAuthRequired] = useState(false)

  const [chats, setChats] = useState([])
  const [activeChatId, setActiveChatId] = useState(null)
  const [messages, setMessages] = useState([])
  const [files, setFiles] = useState([])
  const [outputs, setOutputs] = useState([])
  const [loading, setLoading] = useState(false)
  const [input, setInput] = useState('')
  const [activeTab, setActiveTab] = useState('chat')
  const [selectedFile, setSelectedFile] = useState(null)
  const [fileContent, setFileContent] = useState('')
  const [openTabs, setOpenTabs] = useState([])
  const [pendingApproval, setPendingApproval] = useState(null)
  const [attachments, setAttachments] = useState([])
  const [isDragging, setIsDragging] = useState(false)
  const [thinkingLevel, setThinkingLevel] = useState('none')
  const [pendingOutputFiles, setPendingOutputFiles] = useState([])
  const [agentStatus, setAgentStatus] = useState(null)  // リアルタイムステータス
  const [diffResults, setDiffResults] = useState({})  // path -> {added, removed, diff}
  const setMessagesRef = useRef(null)
  setMessagesRef.current = setMessages
  const textareaRef = useRef(null)

  // 認証チェック
  useEffect(() => {
    fetch(apiUrl('/api/auth/required')).then(r => r.json()).then(d => {
      if (!d.required) {
        setAuthed(true)
        setAuthRequired(false)
        setAuthChecked(true)
      } else {
        setAuthRequired(true)
        const saved = sessionStorage.getItem('codesigner_auth')
        if (saved) {
          fetch(apiUrl('/api/auth'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ passcode: saved })
          }).then(r => {
            if (r.ok) setAuthed(true)
            setAuthChecked(true)
          })
        } else {
          setAuthChecked(true)
        }
      }
    }).catch(() => { setAuthed(true); setAuthChecked(true) })
  }, [])

  const { connected, send, on } = useAgent(authed ? activeChatId : null)

  // チャット読み込み
  useEffect(() => {
    if (!authed) return
    fetch(apiUrl('/api/chats')).then(r => r.json()).then(async d => {
      const chats = d.chats || []
      if (chats.length > 0) {
        setChats(chats)
        selectChat(chats[0].id)
      } else {
        const r = await fetch(apiUrl('/api/chats'), { method: 'POST' })
        const chat = await r.json()
        setChats([chat])
        selectChat(chat.id)
      }
    })
  }, [authed])

  // WS handlers
  useEffect(() => {
    on('ready', (msg) => {
      const chat = msg.chat
      // DBの全メッセージ（tool_call/tool_result含む）を復元
      const rebuilt = []
      let dbIdx = 0
      const addToolEvent = (type, tool, data) => {
        if (type === 'tool_call') {
          const last = rebuilt[rebuilt.length - 1]
          if (last && last.type === 'tool_group') {
            last.items.push({ tool, args: data })
          } else {
            rebuilt.push({ type: 'tool_group', items: [{ tool, args: data }] })
          }
        } else {
          // tool_result: 直近のtool_groupで該当ツールにresultをセット
          for (let i = rebuilt.length - 1; i >= 0; i--) {
            if (rebuilt[i].type === 'tool_group') {
              const items = rebuilt[i].items
              for (let j = items.length - 1; j >= 0; j--) {
                if (items[j].tool === tool && items[j].result === undefined) {
                  items[j] = { ...items[j], result: data }
                  break
                }
              }
              break
            }
          }
        }
      }
      for (const m of chat.messages) {
        if (m.msg_type === 'tool_call' || m.msg_type === 'tool_result') {
          try {
            const parsed = JSON.parse(m.content)
            if (m.msg_type === 'tool_call') addToolEvent('tool_call', parsed.tool, parsed.args)
            else addToolEvent('tool_result', parsed.tool, parsed.result)
          } catch {}
        } else if (m.role === 'tool') {
          try {
            const parsed = JSON.parse(m.content)
            if (parsed.args !== undefined) addToolEvent('tool_call', parsed.tool, parsed.args)
            else if (parsed.result !== undefined) addToolEvent('tool_result', parsed.tool, parsed.result)
          } catch {}
        } else if (m.role === 'user') {
          rebuilt.push({ type: 'user', content: m.content, dbIndex: dbIdx })
          dbIdx++
        } else if (m.role === 'assistant') {
          rebuilt.push({ type: 'text', content: m.content })
        }
      }
      setMessages(rebuilt)
      if (msg.thinking_level) setThinkingLevel(msg.thinking_level)
      refreshFiles(chat.id)
      refreshOutputs(chat.id)
    })
    on('thinking_level', (msg) => setThinkingLevel(msg.level))
    on('system_msg', (msg) => {
      setMessages(prev => [...prev, { type: 'system', content: msg.content }])
    })
    on('stream', (msg) => {
      setMessages(prev => {
        const last = prev[prev.length - 1]
        if (last && last.type === 'streaming') {
          return [...prev.slice(0, -1), { ...last, content: last.content + msg.content }]
        }
        return [...prev.filter(m => m.type !== 'thinking'), { type: 'streaming', content: msg.content }]
      })
    })
    on('stream_end', () => {
      setMessages(prev => {
        const last = prev[prev.length - 1]
        if (last && last.type === 'streaming') {
          return [...prev.slice(0, -1), { type: 'text', content: last.content }]
        }
        return prev
      })
    })
    on('text', (msg) => {
      setMessages(prev => [...prev.filter(m => m.type !== 'thinking' && m.type !== 'streaming'), { type: 'text', content: msg.content }])
    })
    on('tool_call', (msg) => {
      setMessages(prev => {
        const withoutThinking = prev.filter(m => m.type !== 'thinking')
        const last = withoutThinking[withoutThinking.length - 1]
        // 直前がtool_groupならそこに追加
        if (last && last.type === 'tool_group') {
          const updated = [...withoutThinking]
          updated[updated.length - 1] = {
            ...last,
            items: [...last.items, { tool: msg.tool, args: msg.args }]
          }
          return updated
        }
        // 新規グループ作成
        return [...withoutThinking, { type: 'tool_group', items: [{ tool: msg.tool, args: msg.args }] }]
      })
    })
    on('cmd_stream', (msg) => {
      setMessages(prev => {
        // tool_groupの最後のrun_commandにストリーミングを追記
        const last = prev[prev.length - 1]
        if (last && last.type === 'tool_group') {
          const items = [...last.items]
          const lastCmd = [...items].reverse().find(t => t.tool === 'run_command')
          if (lastCmd) {
            const idx = items.lastIndexOf(lastCmd)
            items[idx] = { ...lastCmd, streaming: [...(lastCmd.streaming || []), msg.line] }
            return [...prev.slice(0, -1), { ...last, items }]
          }
        }
        return prev
      })
    })
    on('tool_result', (msg) => {
      setMessages(prev => {
        // tool_groupの該当ツールにresultをセット
        const updated = [...prev]
        for (let i = updated.length - 1; i >= 0; i--) {
          if (updated[i].type === 'tool_group') {
            const items = [...updated[i].items]
            // resultのないものから逆順で探して最初にヒットしたものを更新
            for (let j = items.length - 1; j >= 0; j--) {
              if (items[j].tool === msg.tool && items[j].result === undefined) {
                items[j] = { ...items[j], result: msg.result }
                break
              }
            }
            updated[i] = { ...updated[i], items }
            break
          }
        }
        // copy_to_output/write_file/apply_diffの後処理
        if (msg.tool === 'copy_to_output' && msg.result?.success) {
          refreshOutputs()
          const fname = msg.result.output_path?.split('/').pop() || ''
          if (fname) setPendingOutputFiles(p => [...p, { name: fname, path: msg.result.output_path }])
        }
        if (['write_file','apply_diff'].includes(msg.tool) && msg.result?.output_copied) {
          refreshOutputs()
          const outPath = msg.result.output_copied
          const fname = outPath.split('/').pop()
          if (fname) setPendingOutputFiles(p => [...p, { name: fname, path: outPath }])
        }
        if (['write_file','apply_diff','delete_file'].includes(msg.tool) && msg.result?.success) refreshFiles()
        return updated
      })
    })
    on('diff_result', (msg) => {
      setDiffResults(prev => ({ ...prev, [msg.path]: { path: msg.path, added: msg.added, removed: msg.removed, diff: msg.diff } }))
    })
    on('approval_request', (msg) => {
      setMessages(prev => [...prev.filter(m => m.type !== 'thinking')])
      setPendingApproval(msg)
    })
    on('title_updated', (msg) => {
      setChats(prev => prev.map(c => c.id === activeChatId ? { ...c, title: msg.title } : c))
    })
    on('agent_status', (msg) => {
      setAgentStatus(msg.label || null)
    })
    on('done', () => {
      setAgentStatus(null)
      setLoading(false)
      // 全tool_groupのresultがundefinedのitemsを強制完了にする（スピナー止める）
      setMessages(prev => prev.map(msg => {
        if (msg.type !== 'tool_group') return msg
        const items = msg.items.map(t =>
          t.result === undefined ? { ...t, result: { skipped: true } } : t
        )
        return { ...msg, items }
      }))
      refreshFiles()
      refreshOutputs()
      setPendingOutputFiles(prev => {
        if (prev.length > 0) {
          setMessages(msgs => [...msgs, { type: 'output_files', files: prev }])
        }
        return []
      })
    })
    on('error', (msg) => {
      setLoading(false)
      setMessages(prev => [...prev.filter(m => m.type !== 'thinking' && m.type !== 'streaming'), { type: 'error', content: msg.content }])
    })
  }, [on, activeChatId])

  const refreshFiles = useCallback(async (cid) => {
    const id = cid ?? activeChatId
    if (!id) return
    try {
      const r = await fetch(apiUrl(`/api/files?chat_id=${encodeURIComponent(id)}`))
      const data = await r.json()
      if (data.files) setFiles(data.files)
    } catch {}
  }, [activeChatId])

  const refreshOutputs = useCallback(async (cid) => {
    const id = cid ?? activeChatId
    if (!id) return
    try {
      const r = await fetch(apiUrl(`/api/outputs?chat_id=${encodeURIComponent(id)}`))
      const data = await r.json()
      if (data.files) setOutputs(data.files)
    } catch {}
  }, [activeChatId])

  function selectChat(id) {
    setActiveChatId(id)
    setMessages([])
    setFiles([])
    setOutputs([])
    setActiveTab('chat')
    setSelectedFile(null)
    setOpenTabs([])
    setPendingOutputFiles([])
    setLoading(false)
    setPendingApproval(null)
    try { setDiffResults(JSON.parse(sessionStorage.getItem('diffResults_' + id) || '{}')) } catch { setDiffResults({}) }
  }

  async function createChat() {
    const r = await fetch(apiUrl('/api/chats'), { method: 'POST' })
    const chat = await r.json()
    setChats(prev => [chat, ...prev])
    selectChat(chat.id)
  }

  async function deleteChat(id) {
    if (!confirm('Delete this chat and its workspace?')) return
    await fetch(apiUrl(`/api/chats/${id}`), { method: 'DELETE' })
    setChats(prev => prev.filter(c => c.id !== id))
    if (activeChatId === id) {
      const remaining = chats.filter(c => c.id !== id)
      if (remaining.length > 0) selectChat(remaining[0].id)
      else { setActiveChatId(null); setMessages([]); setFiles([]); setOutputs([]) }
    }
  }

  async function renameChat(id, title) {
    await fetch(apiUrl(`/api/chats/${id}`), { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title }) })
    setChats(prev => prev.map(c => c.id === id ? { ...c, title } : c))
  }

  const sendMessage = useCallback((overrideText) => {
    const text = (overrideText ?? input).trim()
    if (!text || loading || !connected || !activeChatId) return
    setDiffResults({})  // タスク開始時にdiffをリセット
    try { sessionStorage.removeItem('diffResults_' + activeChatId) } catch {}
    setMessages(prev => {
      const userCount = prev.filter(m => m.type === 'user').length
      return [...prev.filter(m => m.type !== 'file_diff'), { type: 'user', content: text, dbIndex: userCount }, { type: 'thinking' }]
    })
    setInput('')
    setAttachments([])
    setLoading(true)
    if (textareaRef.current) textareaRef.current.style.height = 'auto'
    send({ type: 'message', content: text })
  }, [input, loading, connected, activeChatId, send])

  const sendEdit = useCallback((text, dbIndex) => {
    if (!text || loading || !connected || !activeChatId) return
    setMessages(prev => {
      const cutUiIdx = dbIndex !== null
        ? prev.findIndex(m => m.type === 'user' && m.dbIndex === dbIndex)
        : prev.filter(m => m.type === 'user').length - 1
      const truncated = cutUiIdx >= 0 ? prev.slice(0, cutUiIdx) : prev
      return [...truncated, { type: 'user', content: text, dbIndex: dbIndex ?? 0 }, { type: 'thinking' }]
    })
    setLoading(true)
    send({ type: 'edit', content: text, truncate_at: dbIndex })
  }, [loading, connected, activeChatId, send])

  const handleRetry = useCallback((dbIndex) => {
    if (loading || !connected || !activeChatId) return
    setMessages(prev => {
      const cutUiIdx = dbIndex !== null
        ? prev.findIndex(m => m.type === 'user' && m.dbIndex === dbIndex)
        : prev.filter(m => m.type === 'user').length - 1
      const truncated = cutUiIdx >= 0 ? prev.slice(0, cutUiIdx) : prev.slice(0, -1)
      const userMsg = prev.find(m => m.type === 'user' && m.dbIndex === dbIndex)
      if (!userMsg) return prev
      return [...truncated, { ...userMsg }, { type: 'thinking' }]
    })
    setLoading(true)
    send({ type: 'retry', truncate_at: dbIndex })
  }, [loading, connected, activeChatId, send])

  const openFile = useCallback(async (path) => {
    setSelectedFile(path)
    setActiveTab('editor')
    if (!openTabs.includes(path)) setOpenTabs(prev => [...prev, path])
    try {
      const r = await fetch(apiUrl(`/api/file?path=${encodeURIComponent(path)}&chat_id=${encodeURIComponent(activeChatId)}`))
      const data = await r.json()
      setFileContent(data.content ?? data.content_chunk ?? '')
    } catch {}
  }, [activeChatId, openTabs])

  const downloadFile = useCallback((path) => {
    window.open(apiUrl(`/api/download?path=${encodeURIComponent(path)}&chat_id=${encodeURIComponent(activeChatId)}`))
  }, [activeChatId])

  const deleteOutput = useCallback(async (name) => {
    await fetch(apiUrl(`/api/outputs/${encodeURIComponent(name)}?chat_id=${encodeURIComponent(activeChatId)}`), { method: 'DELETE' })
    refreshOutputs()
  }, [activeChatId, refreshOutputs])

  function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage() }
  }

  function handleInput(e) {
    setInput(e.target.value)
    e.target.style.height = 'auto'
    e.target.style.height = Math.min(e.target.scrollHeight, 180) + 'px'
  }

  function handleDragOver(e) { e.preventDefault(); setIsDragging(true) }
  function handleDragLeave() { setIsDragging(false) }
  async function handleDrop(e) {
    e.preventDefault(); setIsDragging(false)
    const files = Array.from(e.dataTransfer.files)
    const uploaded = []
    for (const file of files) {
      const fd = new FormData()
      fd.append('file', file)
      fd.append('chat_id', activeChatId)
      try {
        const res = await fetch(apiUrl('/api/upload'), { method: 'POST', body: fd })
        const data = await res.json()
        if (data.success) {
          uploaded.push(data.path || `input/${file.name}`)
          refreshFiles()
        }
      } catch {}
    }
    if (uploaded.length > 0) {
      setAttachments(prev => [...prev, ...uploaded])
      const fileList = uploaded.map(p => `\`${p}\``).join(', ')
      setInput(prev => prev ? `${prev}\n${fileList} をinputフォルダにアップロードしました。` : `${fileList} をinputフォルダにアップロードしました。`)
      if (textareaRef.current) textareaRef.current.focus()
    }
  }

  function handleApproval(approved) {
    if (!pendingApproval) return
    send({ type: 'approval', call_id: pendingApproval.call_id, approved })
    setPendingApproval(null)
  }

  const lang = selectedFile ? (selectedFile.endsWith('.py') ? 'python' : selectedFile.endsWith('.js') || selectedFile.endsWith('.jsx') ? 'javascript' : selectedFile.endsWith('.ts') ? 'typescript' : selectedFile.endsWith('.json') ? 'json' : selectedFile.endsWith('.md') ? 'markdown' : selectedFile.endsWith('.css') ? 'css' : selectedFile.endsWith('.html') ? 'html' : selectedFile.endsWith('.sh') ? 'shell' : 'plaintext') : 'plaintext'

  // 認証チェック中
  if (!authChecked) return (
    <div className="login-screen">
      <div className="login-card">
        <Loader2 size={24} className="spin" />
      </div>
    </div>
  )

  // 未認証
  if (authRequired && !authed) return <LoginScreen onLogin={() => setAuthed(true)} />

  return (
    <div className="app">
      <ChatSidebar
        chats={chats}
        activeChatId={activeChatId}
        onSelect={selectChat}
        onCreate={createChat}
        onDelete={deleteChat}
        onRename={renameChat}
      />

      <div className="file-sidebar">
        <FileTree
          files={files}
          selected={selectedFile}
          onSelect={openFile}
          onRefresh={() => refreshFiles()}
          onDownload={downloadFile}
        />
        <OutputPanel
          outputs={outputs}
          onRefresh={() => refreshOutputs()}
          onDownload={downloadFile}
          onDelete={deleteOutput}
        />
      </div>

      <div className="main-area">
        <div className="topbar">
          <div className="tabs">
            <button className={`tab ${activeTab === 'chat' ? 'active' : ''}`} onClick={() => setActiveTab('chat')}>
              <MessageSquare size={13} /> Chat
            </button>
            {openTabs.map(tab => (
              <button key={tab} className={`tab ${activeTab === 'editor' && selectedFile === tab ? 'active' : ''}`}
                onClick={() => { setSelectedFile(tab); setActiveTab('editor') }}>
                <File size={13} />
                {tab.split('/').pop()}
                <span className="tab-close" onClick={(e) => {
                  e.stopPropagation()
                  setOpenTabs(prev => prev.filter(t => t !== tab))
                  if (selectedFile === tab) setActiveTab('chat')
                }}><X size={11} /></span>
              </button>
            ))}
          </div>
          <div className="topbar-right">
            <span className={`thinking-badge thinking-${thinkingLevel}`} title="/thinking on|off|auto で切り替え">
              {thinkingLevel === 'high' ? '🧠 Thinking ON' : thinkingLevel === 'auto' ? '🔄 Thinking AUTO' : '⚡ Thinking OFF'}
            </span>
            <div className={`status-indicator ${connected ? 'connected' : ''}`} />
            <span className="status-label">{connected ? 'connected' : 'connecting…'}</span>
          </div>
        </div>

        {activeTab === 'chat' ? (
          <div className="chat-area">
            {!activeChatId ? (
              <div className="empty-chat">
                <span className="empty-icon">⌘</span>
                <h2>Welcome to Codesigner</h2>
                <p>Create a new chat to get started</p>
                <button className="create-chat-btn" onClick={createChat}><Plus size={14} /> New Chat</button>
              </div>
            ) : (
              <>
                <div
                  className={`chat-drop-zone ${isDragging ? 'drag-over' : ''}`}
                  onDragOver={handleDragOver} onDragLeave={handleDragLeave} onDrop={handleDrop}
                  style={{display:'contents'}}
                >
                <MessageList messages={messages} onRetry={handleRetry} onEditSend={sendEdit} activeChatId={activeChatId} diffResults={diffResults} />
                {pendingApproval && (
                  <div className="approval-overlay">
                    <ApprovalCard msg={pendingApproval} onApprove={() => handleApproval(true)} onReject={() => handleApproval(false)} />
                  </div>
                )}
                {isDragging && (
                  <div className="drop-overlay">
                    <div className="drop-overlay-inner">📎 ファイルをドロップして添付</div>
                  </div>
                )}
                {loading && agentStatus && (
                  <div className="agent-status-bar">
                    <Loader2 size={11} className="spin" />
                    <span>{agentStatus}</span>
                  </div>
                )}
                <div className="input-area">
                  {attachments.length > 0 && (
                    <div className="attachments-row">
                      {attachments.map(f => (
                        <span key={f} className="attachment-badge"><Paperclip size={10} /> {f}</span>
                      ))}
                    </div>
                  )}
                  <div className="input-row">
                    <textarea
                      ref={textareaRef}
                      className="chat-input"
                      value={input}
                      placeholder="Ask Codesigner… (Enter to send, Shift+Enter for newline)"
                      rows={1}
                      onChange={handleInput}
                      onKeyDown={handleKeyDown}
                      disabled={loading || !connected}
                    />
                    <button className="send-btn" onClick={sendMessage} disabled={!input.trim() || loading || !connected}>
                      {loading ? <Loader2 size={14} className="spin" /> : <Send size={14} />}
                    </button>
                  </div>
                </div>
                </div>
              </>
            )}
          </div>
        ) : (
          <EditorArea selectedFile={selectedFile} fileContent={fileContent} lang={lang} />
        )}
      </div>
    </div>
  )
}
