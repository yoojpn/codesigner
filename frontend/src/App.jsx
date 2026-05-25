import React, { useState, useEffect, useRef, useCallback } from 'react'
import { ChevronRight, ChevronDown, File, Folder, FolderOpen, X, Check, Terminal, Search, RefreshCw, Download, Plus, Trash2, Send, Loader2, AlertTriangle, Globe, GitBranch, Code2, Cpu } from 'lucide-react'
import MonacoEditor from '@monaco-editor/react'
import './App.css'

// ---- WebSocket Hook ----
function useAgent(wsUrl) {
  const ws = useRef(null)
  const [connected, setConnected] = useState(false)
  const handlers = useRef({})

  const connect = useCallback(() => {
    if (ws.current?.readyState === WebSocket.OPEN) return
    const sock = new WebSocket(wsUrl)
    sock.onopen = () => setConnected(true)
    sock.onclose = () => { setConnected(false); setTimeout(connect, 3000) }
    sock.onerror = () => sock.close()
    sock.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data)
        handlers.current[msg.type]?.(msg)
      } catch {}
    }
    ws.current = sock
  }, [wsUrl])

  useEffect(() => { connect(); return () => ws.current?.close() }, [connect])

  const send = useCallback((data) => {
    if (ws.current?.readyState === WebSocket.OPEN) ws.current.send(JSON.stringify(data))
  }, [])

  const on = useCallback((type, fn) => { handlers.current[type] = fn }, [])

  return { connected, send, on }
}

// ---- File Tree ----
function FileTree({ files, selected, onSelect, onRefresh }) {
  const [expanded, setExpanded] = useState(new Set(['.']))
  const tree = buildTree(files)

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

  function toggle(path) {
    setExpanded(s => { const n = new Set(s); n.has(path) ? n.delete(path) : n.add(path); return n })
  }

  function renderNode(node, path = '', depth = 0) {
    return Object.entries(node)
      .filter(([k]) => k !== '__meta')
      .sort(([a, av], [b, bv]) => {
        const aDir = av.__meta?.isDir; const bDir = bv.__meta?.isDir
        if (aDir && !bDir) return -1; if (!aDir && bDir) return 1
        return a.localeCompare(b)
      })
      .map(([key, val]) => {
        const meta = val.__meta || { name: key, isDir: false }
        const fullPath = path ? `${path}/${key}` : key
        const isDir = meta.isDir || Object.keys(val).filter(k => k !== '__meta').length > 0
        const isExp = expanded.has(fullPath)
        const isSelected = selected === fullPath

        return (
          <div key={fullPath}>
            <div
              className={`tree-item ${isSelected ? 'selected' : ''}`}
              style={{ paddingLeft: `${depth * 12 + 8}px` }}
              onClick={() => isDir ? toggle(fullPath) : onSelect(fullPath)}
            >
              {isDir
                ? (isExp ? <ChevronDown size={12} className="tree-icon" /> : <ChevronRight size={12} className="tree-icon" />)
                : <span style={{width:12,display:'inline-block'}} />}
              {isDir
                ? (isExp ? <FolderOpen size={14} className="file-icon folder-open" /> : <Folder size={14} className="file-icon folder" />)
                : <File size={14} className={`file-icon ${getFileColor(key)}`} />}
              <span className="tree-label">{key}</span>
            </div>
            {isDir && isExp && renderNode(val, fullPath, depth + 1)}
          </div>
        )
      })
  }

  return (
    <div className="file-tree">
      <div className="panel-header">
        <span>EXPLORER</span>
        <button className="icon-btn" onClick={onRefresh} title="Refresh"><RefreshCw size={13} /></button>
      </div>
      <div className="tree-body">
        {files.length === 0
          ? <div className="empty-state">No files yet</div>
          : renderNode(tree)}
      </div>
    </div>
  )
}

function getFileColor(name) {
  const ext = name.split('.').pop()?.toLowerCase()
  const map = { js:'js',jsx:'js',ts:'ts',tsx:'ts',py:'py',json:'json',md:'md',css:'css',html:'html',sh:'sh' }
  return `ext-${map[ext] || 'default'}`
}

// ---- Message Components ----
function ToolCallBadge({ tool, args }) {
  const icons = { run_command: Terminal, web_search: Globe, write_file: Code2, read_file: File, apply_diff: GitBranch, list_files: Folder, search_files: Search, delete_file: Trash2 }
  const Icon = icons[tool] || Cpu
  const colors = { run_command:'yellow', web_search:'accent', write_file:'green', apply_diff:'purple', delete_file:'red' }
  const color = colors[tool] || 'text2'

  return (
    <div className={`tool-call tool-${color}`}>
      <div className="tool-header">
        <Icon size={13} />
        <span className="tool-name">{tool}</span>
        {tool === 'run_command' && <code className="tool-cmd">{args.command}</code>}
        {tool === 'write_file' && <code className="tool-cmd">{args.path}</code>}
        {tool === 'read_file' && <code className="tool-cmd">{args.path}</code>}
        {tool === 'apply_diff' && <code className="tool-cmd">{args.path}</code>}
        {tool === 'web_search' && <code className="tool-cmd">{args.query}</code>}
      </div>
    </div>
  )
}

function ToolResultView({ tool, result }) {
  const [expanded, setExpanded] = useState(false)

  if (result.cancelled) return <div className="tool-result cancelled"><X size={12} /> Cancelled</div>
  if (result.error) return <div className="tool-result error"><AlertTriangle size={12} /> {result.error}</div>

  if (tool === 'run_command') {
    const ok = result.returncode === 0
    return (
      <div className={`tool-result cmd-result ${ok ? 'success' : 'error'}`}>
        <div className="result-header" onClick={() => setExpanded(!expanded)}>
          {ok ? <Check size={12} /> : <X size={12} />}
          <span>Exit {result.returncode}</span>
          {(result.stdout || result.stderr) && <ChevronRight size={12} className={`expand-icon ${expanded?'expanded':''}`} />}
        </div>
        {expanded && (result.stdout || result.stderr) && (
          <pre className="cmd-output">{result.stdout}{result.stderr && <span className="stderr">{result.stderr}</span>}</pre>
        )}
      </div>
    )
  }

  if (tool === 'apply_diff' && result.diff) {
    return (
      <div className="tool-result diff-result">
        <div className="result-header" onClick={() => setExpanded(!expanded)}>
          <GitBranch size={12} />
          <span>Diff applied</span>
          <ChevronRight size={12} className={`expand-icon ${expanded?'expanded':''}`} />
        </div>
        {expanded && <DiffView diff={result.diff} />}
      </div>
    )
  }

  if (tool === 'write_file') return <div className="tool-result success"><Check size={12} /> Written: {result.path} ({result.bytes}B)</div>
  if (tool === 'list_files') return <div className="tool-result info"><File size={12} /> {result.files?.length ?? 0} files</div>
  if (tool === 'web_search') return (
    <div className="tool-result info">
      <Globe size={12} /> {result.results?.length ?? 0} results for "{result.query}"
    </div>
  )

  return <div className="tool-result success"><Check size={12} /> Done</div>
}

function DiffView({ diff }) {
  if (!diff) return null
  const lines = diff.split('\n')
  return (
    <pre className="diff-view">
      {lines.map((line, i) => (
        <div key={i} className={`diff-line ${line.startsWith('+') && !line.startsWith('+++') ? 'add' : line.startsWith('-') && !line.startsWith('---') ? 'del' : line.startsWith('@@') ? 'hunk' : ''}`}>
          {line}
        </div>
      ))}
    </pre>
  )
}

function ApprovalRequest({ tool, args, onApprove, onReject }) {
  const dangerous = tool === 'delete_file' || (tool === 'run_command' && /rm |sudo |chmod/.test(args.command || ''))
  return (
    <div className={`approval-box ${dangerous ? 'danger' : ''}`}>
      <div className="approval-header">
        <AlertTriangle size={14} />
        <span>Allow <strong>{tool}</strong>?</span>
      </div>
      {tool === 'run_command' && <code className="approval-cmd">$ {args.command}</code>}
      {tool === 'write_file' && <code className="approval-cmd">Write to {args.path}</code>}
      {tool === 'apply_diff' && <code className="approval-cmd">Patch {args.path}</code>}
      {tool === 'delete_file' && <code className="approval-cmd danger-text">Delete {args.path}</code>}
      <div className="approval-actions">
        <button className="btn btn-approve" onClick={onApprove}><Check size={13} /> Allow</button>
        <button className="btn btn-reject" onClick={onReject}><X size={13} /> Reject</button>
      </div>
    </div>
  )
}

function MessageBubble({ msg, onApprove, onReject }) {
  if (msg.type === 'user') return (
    <div className="message user-msg">
      <div className="msg-content">{msg.content}</div>
    </div>
  )

  if (msg.type === 'tool_call') return <ToolCallBadge tool={msg.tool} args={msg.args} />
  if (msg.type === 'tool_result') return <ToolResultView tool={msg.tool} result={msg.result} />
  if (msg.type === 'approval') return <ApprovalRequest tool={msg.tool} args={msg.args} onApprove={() => onApprove(msg.callId)} onReject={() => onReject(msg.callId)} />

  if (msg.type === 'text') return (
    <div className="message agent-msg">
      <div className="msg-avatar"><Cpu size={13} /></div>
      <div className="msg-content markdown-text">{msg.content}</div>
    </div>
  )

  if (msg.type === 'error') return (
    <div className="message error-msg">
      <AlertTriangle size={14} />
      <span>{msg.content}</span>
    </div>
  )

  return null
}

// ---- Main App ----
export default function App() {
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [files, setFiles] = useState([])
  const [selectedFile, setSelectedFile] = useState(null)
  const [fileContent, setFileContent] = useState('')
  const [activeTab, setActiveTabs] = useState('chat') // chat | editor
  const [pendingApprovals, setPendingApprovals] = useState({})
  const [sidebarOpen, setSidebarOpen] = useState(true)

  const bottomRef = useRef(null)
  const inputRef = useRef(null)
  const approvalResolvers = useRef({})

  const wsUrl = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`
  const { connected, send, on } = useAgent(wsUrl)

  const addMessage = useCallback((msg) => {
    setMessages(m => [...m, { ...msg, id: Date.now() + Math.random() }])
  }, [])

  useEffect(() => {
    on('text', (msg) => addMessage({ type: 'text', content: msg.content }))
    on('tool_call', (msg) => addMessage({ type: 'tool_call', tool: msg.tool, args: msg.args }))
    on('tool_result', (msg) => {
      addMessage({ type: 'tool_result', tool: msg.tool, result: msg.result })
      if (msg.tool === 'list_files' && msg.result.files) setFiles(msg.result.files)
    })
    on('approval_request', (msg) => {
      const callId = msg.call_id
      addMessage({ type: 'approval', tool: msg.tool, args: msg.args, callId })
      setPendingApprovals(p => ({ ...p, [callId]: true }))
    })
    on('done', () => { setLoading(false); refreshFiles() })
    on('error', (msg) => { addMessage({ type: 'error', content: msg.content }); setLoading(false) })
  }, [on, addMessage])

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages])

  const refreshFiles = useCallback(async () => {
    try {
      const r = await fetch('/api/files')
      const data = await r.json()
      if (data.files) setFiles(data.files)
    } catch {}
  }, [])

  useEffect(() => { if (connected) refreshFiles() }, [connected, refreshFiles])

  const openFile = useCallback(async (path) => {
    setSelectedFile(path)
    setActiveTab('editor')
    try {
      const r = await fetch(`/api/file?path=${encodeURIComponent(path)}`)
      const data = await r.json()
      setFileContent(data.content || '')
    } catch {}
  }, [])

  const downloadFile = useCallback(() => {
    if (selectedFile) window.open(`/api/download?path=${encodeURIComponent(selectedFile)}`)
  }, [selectedFile])

  const submit = useCallback(() => {
    const text = input.trim()
    if (!text || loading || !connected) return
    addMessage({ type: 'user', content: text })
    setInput('')
    setLoading(true)
    send({ type: 'message', content: text })
    inputRef.current?.focus()
  }, [input, loading, connected, send, addMessage])

  const handleApprove = useCallback((callId) => {
    send({ type: 'approval', approved: true, call_id: callId })
    setPendingApprovals(p => { const n = {...p}; delete n[callId]; return n })
  }, [send])

  const handleReject = useCallback((callId) => {
    send({ type: 'approval', approved: false, call_id: callId })
    setPendingApprovals(p => { const n = {...p}; delete n[callId]; return n })
  }, [send])

  const lang = selectedFile ? selectedFile.split('.').pop() : 'plaintext'
  const monacoLang = { js:'javascript', jsx:'javascript', ts:'typescript', tsx:'typescript', py:'python', md:'markdown', sh:'shell', json:'json', html:'html', css:'css' }[lang] || 'plaintext'

  return (
    <div className="app">
      {/* Title Bar */}
      <div className="titlebar">
        <div className="titlebar-left">
          <Code2 size={16} className="logo-icon" />
          <span className="app-name">codesigner</span>
        </div>
        <div className="titlebar-center">
          {selectedFile && activeTab === 'editor' && (
            <span className="active-file">{selectedFile}</span>
          )}
        </div>
        <div className="titlebar-right">
          <div className={`status-dot ${connected ? 'online' : 'offline'}`} />
          <span className="status-text">{connected ? 'connected' : 'reconnecting...'}</span>
        </div>
      </div>

      <div className="workspace">
        {/* Sidebar */}
        {sidebarOpen && (
          <div className="sidebar">
            <FileTree files={files} selected={selectedFile} onSelect={openFile} onRefresh={refreshFiles} />
          </div>
        )}

        {/* Main Area */}
        <div className="main-area">
          {/* Tab Bar */}
          <div className="tabs">
            <button className={`tab ${activeTab === 'chat' ? 'active' : ''}`} onClick={() => setActiveTab('chat')}>
              <Cpu size={13} /> Chat
            </button>
            {selectedFile && (
              <button className={`tab ${activeTab === 'editor' ? 'active' : ''}`} onClick={() => setActiveTab('editor')}>
                <File size={13} /> {selectedFile.split('/').pop()}
                <button className="tab-action" onClick={(e) => { e.stopPropagation(); downloadFile() }} title="Download"><Download size={11} /></button>
              </button>
            )}
          </div>

          {/* Chat Panel */}
          {activeTab === 'chat' && (
            <div className="chat-area">
              <div className="messages">
                {messages.length === 0 && (
                  <div className="welcome">
                    <Code2 size={32} className="welcome-icon" />
                    <h2>Codesigner</h2>
                    <p>AI coding assistant. Ask me to write, edit, or run code.</p>
                    <div className="suggestions">
                      {['Create a Python web scraper', 'Set up a Node.js project', 'Write a shell script', 'Search for recent docs'].map(s => (
                        <button key={s} className="suggestion" onClick={() => { setInput(s); inputRef.current?.focus() }}>{s}</button>
                      ))}
                    </div>
                  </div>
                )}
                {messages.map(msg => (
                  <MessageBubble key={msg.id} msg={msg} onApprove={handleApprove} onReject={handleReject} />
                ))}
                {loading && (
                  <div className="message agent-msg loading">
                    <div className="msg-avatar"><Cpu size={13} /></div>
                    <div className="thinking"><Loader2 size={13} className="spin" /><span>thinking...</span></div>
                  </div>
                )}
                <div ref={bottomRef} />
              </div>

              <div className="input-area">
                <textarea
                  ref={inputRef}
                  className="chat-input"
                  value={input}
                  onChange={e => setInput(e.target.value)}
                  onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit() } }}
                  placeholder="Ask Codesigner... (Enter to send, Shift+Enter for newline)"
                  rows={1}
                  disabled={loading}
                />
                <button className={`send-btn ${loading ? 'loading' : ''}`} onClick={submit} disabled={loading || !input.trim()}>
                  {loading ? <Loader2 size={15} className="spin" /> : <Send size={15} />}
                </button>
              </div>
            </div>
          )}

          {/* Editor Panel */}
          {activeTab === 'editor' && selectedFile && (
            <div className="editor-area">
              <MonacoEditor
                height="100%"
                language={monacoLang}
                value={fileContent}
                theme="vs-dark"
                onChange={v => setFileContent(v)}
                options={{
                  fontSize: 13,
                  fontFamily: "'SF Mono', 'Fira Code', 'Cascadia Code', Consolas, monospace",
                  fontLigatures: true,
                  minimap: { enabled: false },
                  scrollBeyondLastLine: false,
                  wordWrap: 'on',
                  lineNumbers: 'on',
                  renderWhitespace: 'none',
                  smoothScrolling: true,
                  cursorSmoothCaretAnimation: 'on',
                  padding: { top: 16, bottom: 16 }
                }}
              />
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
