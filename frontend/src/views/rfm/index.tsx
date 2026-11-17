import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/rfm'
const columns = [
  "规则编号",
  "最近消费天界",
  "消费频次界",
  "消费金额界",
  "分层层级",
  "层间流转规则",
  "更新周期",
  "覆盖用户量"
]
const actions = ["提交规则评审", "启用规则", "停用规则"]
const ALLOWED_FROM: Record<string, string> = {
  "提交规则评审": "草稿",
  "启用规则": "规则评审中",
  "停用规则": "已启用",
}
const statuses = ["草稿", "规则评审中", "已启用", "已停用"]

type Row = Record<string, string | number | null | boolean>

type LayerRow = {
  用户编号: string
  分层层级: string
  缓存分层层级: string
  消费频次: number
  最近消费天: number
  与缓存一致: boolean
}

type ResultCheck = {
  规则: Row
  数据快照: string
  分层明细: LayerRow[]
  分层分布: Record<string, number>
  链路: { 环节: string; 对象: string; 状态: string; 结论: string }[]
  依赖断点: string[]
  缓存告警: string[]
  当前数据集: string | null
}

export default function RfmView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [checkId, setCheckId] = useState('')
  const [check, setCheck] = useState<ResultCheck | null>(null)
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('分层规则列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : 'RFM分层列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function loadCheck(ruleId: string) {
    if (!ruleId) return
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/result-check/${ruleId}`)
      const payload = await response.json()
      if (!response.ok) {
        throw new Error(payload.detail ?? '结果回查失败')
      }
      setCheck(payload)
      setCheckId(ruleId)
    } catch (error) {
      setCheck(null)
      setErrorMessage(error instanceof Error ? error.message : '结果回查失败')
    }
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({
          values: { action },
          expected_version: row.version,
          receipt_key: `${row.规则编号}-${action}-${row.version}`,
          remark: action === '停用规则' ? '页面停用' : undefined,
        }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        setErrorMessage(payload.message ?? 'RFM分层动作未生效')
      }
      await reload()
      if (check) {
        await loadCheck(String(check.规则.id))
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : 'RFM分层操作失败')
    }
  }

  const enabledCount = rows.filter((row) => row.status === '已启用').length

  return (
    <section className="page" data-module="rfm">
      <header className="page-head">
        <div>
          <h2>RFM分层管理</h2>
          <p className="page-desc">
            配置RFM用户价值分层规则。状态只能按“草稿 → 规则评审中 → 已启用 → 已停用”前进，
            停用会同步收缩流失名单，并与名单一起成功。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('分层规则登记入口尚未接入审批流')}>
            登记分层规则
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出RFM分层清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        <article className="stat-card"><span className="stat-label">分层规则总数</span><strong className="stat-value">{total}</strong></article>
        <article className="stat-card"><span className="stat-label">已启用规则</span><strong className="stat-value">{enabledCount}</strong></article>
        <article className="stat-card"><span className="stat-label">评审中规则</span><strong className="stat-value">{rows.filter((row) => row.status === '规则评审中').length}</strong></article>
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

      <table className="data-table">
        <thead>
          <tr>
            <th>状态</th>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>规则版本</th>
            <th>评审结论</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              <td>{String(row.status ?? '')}</td>
              {columns.map((column) => <td key={column}>{String(row[column] ?? '—')}</td>)}
              <td>第{String(row.规则版本 ?? 1)}版 / v{String(row.version ?? 1)}</td>
              <td>{String(row.评审结论 || '—')}</td>
              <td className="row-actions">
                {actions.map((action) => {
                  const blocked = String(row.status ?? '') !== ALLOWED_FROM[action]
                    || (action === '启用规则' && row.评审结论 !== '通过')
                  return (
                    <button
                      key={action}
                      className="link"
                      type="button"
                      disabled={blocked}
                      title={blocked ? `${action}只允许从「${ALLOWED_FROM[action]}」进入` : undefined}
                      onClick={() => void runAction(action, row)}
                    >
                      {action}
                    </button>
                  )
                })}
                <button className="link" type="button" onClick={() => void loadCheck(String(row.id))}>
                  结果回查
                </button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 4}>暂无RFM分层数据，可先登记分层规则</td>
            </tr>
          )}
        </tbody>
      </table>

      {check && (
        <section className="detail-panel">
          <header className="detail-head">
            <h3>结果回查 · {String(check.规则.规则编号)} 第{String(check.规则.规则版本)}版</h3>
            <span className="stat-label">数据快照 {check.数据快照} · 当前数据集 {check.当前数据集 ?? '无'}</span>
          </header>
          <div className="detail-columns">
            <article>
              <h4>原调用链</h4>
              <ol className="chain-list">
                {check.链路.map((item) => (
                  <li key={item.环节}>
                    <strong>{item.环节}</strong>
                    <span>{item.对象} · {item.状态}</span>
                    <em>{item.结论}</em>
                  </li>
                ))}
              </ol>
            </article>
            <article>
              <h4>依赖断点</h4>
              {check.依赖断点.length === 0
                ? <p className="ok-text">链路完整，分层结论已落到当前名单。</p>
                : <ul className="issue-list">{check.依赖断点.map((item) => <li key={item}>{item}</li>)}</ul>}
              <h4>分层分布</h4>
              <p>{Object.entries(check.分层分布).map(([key, value]) => `${key} ${value}`).join(' · ')}</p>
              {check.缓存告警.length > 0 && (
                <>
                  <h4>缓存层级告警</h4>
                  <ul className="issue-list">
                    {check.缓存告警.map((item) => <li key={item}>{item}</li>)}
                  </ul>
                </>
              )}
            </article>
          </div>
          <table className="data-table">
            <thead>
              <tr><th>用户编号</th><th>分层层级（按规则重算）</th><th>缓存分层层级</th><th>消费频次</th><th>最近消费天</th><th>与缓存一致</th></tr>
            </thead>
            <tbody>
              {check.分层明细.map((item) => (
                <tr key={item.用户编号}>
                  <td>{item.用户编号}</td>
                  <td>{item.分层层级}</td>
                  <td>{item.缓存分层层级}</td>
                  <td>{item.消费频次}</td>
                  <td>{item.最近消费天}</td>
                  <td>{item.与缓存一致 ? '一致' : '不一致'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条RFM分层记录{checkId ? ` · 正在回查规则 ${checkId}` : ''}</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
