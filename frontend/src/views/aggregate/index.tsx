import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/aggregate'
const TRACE_ENDPOINT = '/api/qualityscore/trace/gap'
const columns = [
  "规则编号",
  "源数据粒度",
  "目标粒度",
  "聚合函数集",
  "数据偏移分钟",
  "补全策略",
  "空值处理",
  "输出数据集",
  "最近评分快照",
  "最近质量总分",
  "最近评分周期"
]
const actions = [
  "提交规则评审",
  "生效规则",
  "废弃规则"
]
const statuses = [
  "草稿",
  "规则评审中",
  "已生效",
  "已废弃"
]
const stats = [
  {
    "label": "聚合规则总数",
    "value": 0
  },
  {
    "label": "已生效规则",
    "value": 0
  },
  {
    "label": "评审中规则",
    "value": 0
  }
]

type Row = Record<string, string | number | null>
type TraceNode = { node: string; present: boolean; detail: string }
type TracePayload = { ok: boolean; reason: string; first_gap: string | null; chain: TraceNode[] }

export default function AggregateView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [trace, setTrace] = useState<TracePayload | null>(null)
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('聚合规则列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '聚合配置列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('聚合配置动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '聚合配置操作失败')
    }
  }

  async function traceGap(row: Row) {
    setErrorMessage('')
    try {
      const dataset = encodeURIComponent(String(row['输出数据集'] ?? ''))
      const response = await request(`${TRACE_ENDPOINT}?dataset=${dataset}`)
      if (!response.ok) throw new Error('调用链追溯失败')
      setTrace(await response.json())
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '调用链追溯失败')
    }
  }

  return (
    <section className="page" data-module="aggregate">
      <header className="page-head">
        <div>
          <h2>聚合配置管理</h2>
          <p className="page-desc">管理数据聚合计算规则；评分快照生成后，输出数据集的评分快照、总分与周期会同步回写到这里。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('聚合规则登记入口尚未接入审批流')}>
            登记聚合规则
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出聚合配置清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      {trace && (
        <div className="trace-panel">
          <strong>{trace.reason}</strong>
          <ol className="trace-chain">
            {trace.chain.map((node) => (
              <li key={node.node} className={node.present ? 'trace-ok' : 'trace-gap'}>
                {node.present ? '✓' : '✗'} {node.node}：{node.detail}
              </li>
            ))}
          </ol>
          <div className="page-actions"><button className="btn" type="button" onClick={() => setTrace(null)}>关闭追溯</button></div>
        </div>
      )}

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => void traceGap(row)}>质量链路追溯</button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无聚合配置数据，可先登记聚合规则</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条聚合配置记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
