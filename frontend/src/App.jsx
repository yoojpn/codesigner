import React, { useState, useEffect, useRef, useCallback } from 'react'
import { ChevronRight, ChevronDown, File, Folder, FolderOpen, X, Check, Terminal,
  Search, RefreshCw, Download, Plus, Trash2, Send, Loader2, AlertTriangle,
  Globe, Code2, Cpu, Paperclip, MessageSquare, Edit2, Copy } from 'lucide-react'
import MonacoEditor from '@monaco-editor/react'
import ReactMarkdown from 'react-markdown'
import './App.css'

// ---- Backend URL ----
const BACKEND_URL = (typeof __BACKEND_URL__ !== 'undefined' && __BACKEND_URL__) ? __BACKEND_URL__.replace(/\/$/, '') : ''
const apiUrl = (path) => `${BACKEND_URL}${path}`

// ---- WebSocket Hook ----
function useAgent(chatId) {
  const ws = useRef(null)
  const [connected, setConnected] = useState(false)
  const handlers = useRef({})

  const connect = useCallback(() => {
    if (!chatId) return
    if (ws.current?.readyState === WebSocket.OPEN) ws.current.close()
    const _backendUrl = BACKEND_URL
    const wsProto = _backendUrl ? (_backendUrl.startsWith('https') ? 'wss' : 'ws') : (location.protocol === 'https:' ? 'wss' : 'ws')
    const wsHost = _backendUrl ? _backendUrl.replace(/^https?:\/\//, '') : location.host
    const sock = new WebSocket(`${wsProto}://${wsHost}/ws/${chatId}`)
    sock.onopen = () => setConnected(true)
    sock.onclose = () => { setConnected(false); setTimeout(connect, 3000) }
    sock.onerror = () => sock.close()
    sock.onmessage = (e) => {
      try { const msg = JSON.parse(e.data); handlers.current[msg.type]?.(msg) } catch {}
    }
    ws.current = sock
  }, [chatId])

  useEffect(() => {
    connect()
    return () => ws.current?.close()
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

function ToolCallBadge({ tool, args }) {
  const icons = { web_search: <Globe size={13} />, run_command: <Terminal size={13} />, apply_diff: <Code2 size={13} />, search_files: <Search size={13} />, fetch_url: <Globe size={13} />, copy_to_output: <Download size={13} /> }
  const icon = icons[tool] || <Code2 size={13} />
  let argsDisplay = ''
  if (tool === 'web_search') argsDisplay = `"${args.query}"`
  else if (tool === 'run_command') argsDisplay = `$ ${args.command}`
  else if (tool === 'fetch_url') argsDisplay = args.url
  else if (tool === 'apply_diff') {
    const diffLines = (args.diff || '').split('\n')
    const added = diffLines.filter(l => l.startsWith('+')).length
    const removed = diffLines.filter(l => l.startsWith('-')).length
    argsDisplay = (
      <span>
        {args.path}
        {(added > 0 || removed > 0) && (
          <span className="diff-inline-stats">
            <span className="diff-stat-add">+{added}</span>
            <span className="diff-stat-del"> −{removed}</span>
          </span>
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
        {tool === 'apply_diff' && args.diff && <DiffView diff={args.diff} />}
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

// ---- Message Renderer ----
function MessageList({ messages, onRetry, onEditSend }) {
  const bottomRef = useRef(null)
  const [editingIdx, setEditingIdx] = useState(null)
  const [editValue, setEditValue] = useState('')
  const [copiedIdx, setCopiedIdx] = useState(null)

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages])

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
              <div className="msg-content markdown-body">
                {msg.type === 'streaming' ? <span className="streaming-text">{msg.content}</span> : <ReactMarkdown>{msg.content}</ReactMarkdown>}
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
            <ReactMarkdown>{msg.content}</ReactMarkdown>
          </div>
        )
        if (msg.type === 'tool_result') return <ToolResultView key={i} tool={msg.tool} result={msg.result} />
        if (msg.type === 'thinking') return (
          <div key={i} className="message agent-msg">
            <div className="msg-avatar"><Cpu size={12} /></div>
            <div className="thinking"><Loader2 size={13} className="spin" /> thinking…</div>
          </div>
        )
        return null
      })}
      <div ref={bottomRef} />
    </div>
  )
}

// ---- Main App ----
export default function App() {
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
  const [lastUserMsg, setLastUserMsg] = useState('')
  const [thinkingLevel, setThinkingLevel] = useState('none')
  const textareaRef = useRef(null)

  const { connected, send, on } = useAgent(activeChatId)

  // Load chats on mount
  useEffect(() => {
    fetch(apiUrl('/api/chats')).then(r => r.json()).then(async d => {
      const chats = d.chats || []
      if (chats.length > 0) {
        setChats(chats)
        selectChat(chats[0].id)
      } else {
        // 初回：チャットを自動作成
        const r = await fetch(apiUrl('/api/chats'), { method: 'POST' })
        const chat = await r.json()
        setChats([chat])
        selectChat(chat.id)
      }
    })
  }, [])

  // WS handlers
  useEffect(() => {
    on('ready', (msg) => {
      const chat = msg.chat
      const rebuilt = []
      let dbIdx = 0
      for (const m of chat.messages) {
        if (m.role === 'user') {
          rebuilt.push({ type: 'user', content: m.content, dbIndex: dbIdx })
          dbIdx++
        } else {
          rebuilt.push({ type: 'text', content: m.content })
        }
      }
      setMessages(rebuilt)
      if (msg.thinking_level) setThinkingLevel(msg.thinking_level)
      refreshFiles(chat.id)
      refreshOutputs(chat.id)
    })
    on('thinking_level', (msg) => {
      setThinkingLevel(msg.level)
    })
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
      setMessages(prev => [...prev.filter(m => m.type !== 'thinking'), { type: 'tool_call', tool: msg.tool, args: msg.args }])
    })
    on('tool_result', (msg) => {
      setMessages(prev => [...prev, { type: 'tool_result', tool: msg.tool, result: msg.result }])
      if (msg.tool === 'copy_to_output' && msg.result?.success) refreshOutputs()
      if (['write_file','apply_diff','delete_file'].includes(msg.tool) && msg.result?.success) refreshFiles()
    })
    on('approval_request', (msg) => {
      setMessages(prev => [...prev.filter(m => m.type !== 'thinking')])
      setPendingApproval(msg)
    })
    on('title_updated', (msg) => {
      setChats(prev => prev.map(c => c.id === activeChatId ? { ...c, title: msg.title } : c))
    })
    on('done', () => {
      setLoading(false)
      refreshFiles()
      refreshOutputs()
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
    setLastUserMsg(text)
    setMessages(prev => {
      const userCount = prev.filter(m => m.type === 'user').length
      return [...prev, { type: 'user', content: text, dbIndex: userCount }, { type: 'thinking' }]
    })
    setInput('')
    setAttachments([])
    setLoading(true)
    if (textareaRef.current) textareaRef.current.style.height = 'auto'
    send({ type: 'message', content: text })
  }, [input, loading, connected, activeChatId, send])

  const sendEdit = useCallback((text, dbIndex) => {
    if (!text || loading || !connected || !activeChatId) return
    // dbIndex以降のメッセージをUIから切り詰めてからやり直す
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
    // dbIndex以降（その返答も含む）をUIから削除してthinkingを追加
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
      setFileContent(data.content || '')
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
      // アップロード完了をAIに自動通知して解説・処理を促す
      const fileList = uploaded.map(p => `\`${p}\``).join(', ')
      const notify = uploaded.length === 1
        ? `${fileList} をinputフォルダにアップロードしました。`
        : `${fileList} をinputフォルダにアップロードしました。`
      setInput(prev => prev ? `${prev}\n${notify}` : notify)
      if (textareaRef.current) textareaRef.current.focus()
    }
  }

  function handleApproval(approved) {
    if (!pendingApproval) return
    send({ type: 'approval', call_id: pendingApproval.call_id, approved })
    setPendingApproval(null)
  }

  const lang = selectedFile ? (selectedFile.endsWith('.py') ? 'python' : selectedFile.endsWith('.js') || selectedFile.endsWith('.jsx') ? 'javascript' : selectedFile.endsWith('.ts') ? 'typescript' : selectedFile.endsWith('.json') ? 'json' : selectedFile.endsWith('.md') ? 'markdown' : selectedFile.endsWith('.css') ? 'css' : selectedFile.endsWith('.html') ? 'html' : selectedFile.endsWith('.sh') ? 'shell' : 'plaintext') : 'plaintext'

  return (
    <div className="app">
      {/* Chat Sidebar */}
      <ChatSidebar
        chats={chats}
        activeChatId={activeChatId}
        onSelect={selectChat}
        onCreate={createChat}
        onDelete={deleteChat}
        onRename={renameChat}
      />

      {/* File Sidebar */}
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

      {/* Main */}
      <div className="main-area">
        {/* Topbar */}
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

        {/* Content */}
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
                <MessageList messages={messages} onRetry={handleRetry} onEditSend={sendEdit} />
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
                </div>{/* chat-drop-zone */}
              </>
            )}
          </div>
        ) : (
          <div className="editor-area">
            {selectedFile && (
              <MonacoEditor
                height="100%"
                language={lang}
                value={fileContent}
                theme="vs-dark"
                options={{ readOnly: true, minimap: { enabled: false }, fontSize: 13, scrollBeyondLastLine: false, lineNumbers: 'on' }}
              />
            )}
          </div>
        )}
      </div>
    </div>
  )
}
