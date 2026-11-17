import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/churn'
const columns = [
  "模型编号",
  "模型名称",
  "特征变量",
  "流失定义天",
  "训练数据集",
  "预测阈值",
  "模型准确率",
  "预警推送频率"
]
const actions = ["发起训练", "部署模型", "下线模型"]
const ALLOWED_FROM: Record<string, string> = {
  "发起训练": "草稿",
  "部署模型": "训练中",
  "下线模型": "已部署",
}
const statuses = ["草稿", "训练中", "已部署", "已下线"]

type Row = Record<string, string | number | null | boolean>

type Member = {
  用户编号: string
  分层层级: string
  流失概率: number | null
  生成依据: string
  命中原因: string
  规则编号: string
  模型编号: string
  模型准确率: number | null
}

type Batch = Record<string, string | number | null>

type Dataset = {
  数据集编号: string
  数据集版本: string
  数据快照: string
  规则编号: string
  规则版本: number
  模型编号: string
  模型版本: number
  模型准确率: number | null
  名单数: number
  生成方式: string
  修复说明: string
  校验摘要: string
}

type CurrentList = {
  批次: Batch | null
  数据集: Dataset | null
  名单数: number
  成员: Member[]
}

type BreakItem = { 环节: string; 原因?: string; 故障边界?: string }

type Reconcile = {
  当前批次: Batch | null
  当前数据集: Dataset | null
  数据快照: string
  有效规则: string | null
  有效模型: string | null
  链路: { 环节: string; 结论: string; [key: string]: unknown }[]
  依赖断点: BreakItem[]
  需要修复: boolean
  历史名单: Batch[]
}

type RepairResult = {
  ok: boolean
  repaired: boolean
  message?: string
  数据集?: Dataset
  复核?: { 一致: boolean; 复核码: string; 问题: string[] }
  消除错误?: { 类型: string; 修复前: string | number; 修复后: string | number; 移出成员?: string[]; 新增成员?: string[] }[]
}

export default function ChurnView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [current, setCurrent] = useState<CurrentList | null>(null)
  const [reconcile, setReconcile] = useState<Reconcile | null>(null)
  const [repair, setRepair] = useState<RepairResult | null>(null)
  const [busy, setBusy] = useState(false)
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    const query = new URLSearchParams(filters).toString()
    try {
      const [listRes, currentRes, reconcileRes] = await Promise.all([
        request(`${ENDPOINT}?${query}`),
        request(`${ENDPOINT}/current-list`),
        request(`${ENDPOINT}/reconcile`),
      ])
      if (!listRes.ok) {
        throw new Error('预警模型列表读取失败')
      }
      const payload = await listRes.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      setCurrent(currentRes.ok ? await currentRes.json() : null)
      setReconcile(reconcileRes.ok ? await reconcileRes.json() : null)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '流失预警列表读取失败')
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
        body: JSON.stringify({
          values: { action },
          expected_version: row.version,
          receipt_key: `${row.模型编号}-${action}-${row.version}`,
        }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        setErrorMessage(payload.message ?? '流失预警动作未生效')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '流失预警操作失败')
    }
  }

  async function repairList() {
    setBusy(true)
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/repair`, {
        method: 'POST',
        body: JSON.stringify({ values: { operator: '值班管理员' }, receipt_key: `repair-${Date.now()}` }),
      })
      const payload = await response.json()
      if (!response.ok) {
        throw new Error(payload.detail ?? '修复未生效')
      }
      setRepair(payload)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '修复失败')
    } finally {
      setBusy(false)
    }
  }

  const deployedCount = rows.filter((row) => row.status === '已部署').length
  const trainingCount = rows.filter((row) => row.status === '训练中').length
  const accuracy = current?.数据集?.模型准确率

  return (
    <section className="page" data-module="churn">
      <header className="page-head">
        <div>
          <h2>流失预警管理</h2>
          <p className="page-desc">
            管理用户流失预警模型。模型只能在“训练完成”后部署；部署与规则停用会共同驱动当前名单，
            同一批次只保留一份完整的当前名单。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" disabled={busy} onClick={() => void repairList()}>
            修复流失名单
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出流失预警清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        <article className="stat-card"><span className="stat-label">预警模型总数</span><strong className="stat-value">{total}</strong></article>
        <article className="stat-card"><span className="stat-label">已部署模型</span><strong className="stat-value">{deployedCount}</strong></article>
        <article className="stat-card"><span className="stat-label">训练中模型</span><strong className="stat-value">{trainingCount}</strong></article>
        <article className="stat-card">
          <span className="stat-label">当前名单模型准确率</span>
          <strong className="stat-value">{accuracy ?? '—'}</strong>
        </article>
        <article className="stat-card"><span className="stat-label">当前名单人数</span><strong className="stat-value">{current?.名单数 ?? 0}</strong></article>
      </div>

      {reconcile && (
        <section className={`banner ${reconcile.需要修复 ? 'banner-warn' : 'banner-ok'}`}>
          <div>
            <strong>{reconcile.需要修复 ? '当前名单存在依赖断点' : '当前名单已对齐'}</strong>
            <span>
              批次 {String(reconcile.当前批次?.批次编号 ?? '无')} · 数据集 {reconcile.当前数据集?.数据集编号 ?? '无'}
              {' '}· 数据快照 {reconcile.数据快照} · 有效规则 {reconcile.有效规则 ?? '无'} · 有效模型 {reconcile.有效模型 ?? '无'}
            </span>
          </div>
          {reconcile.需要修复 && (
            <ul className="issue-list">
              {reconcile.依赖断点.map((item, index) => (
                <li key={`${item.环节}-${index}`}>
                  <strong>{item.环节}</strong>{item.原因}{item.故障边界 ? `（${item.故障边界}）` : ''}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

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
            <th>模型版本</th>
            <th>训练结论</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              <td>{String(row.status ?? '')}</td>
              {columns.map((column) => <td key={column}>{String(row[column] ?? '—')}</td>)}
              <td>第{String(row.模型版本 ?? 1)}版 / v{String(row.version ?? 1)}</td>
              <td>{String(row.训练结论 || '—')}</td>
              <td className="row-actions">
                {actions.map((action) => {
                  const blocked = String(row.status ?? '') !== ALLOWED_FROM[action]
                    || (action === '部署模型' && row.训练结论 !== '已完成')
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
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 4}>暂无流失预警数据，可先登记预警模型</td>
            </tr>
          )}
        </tbody>
      </table>

      <section className="detail-panel">
        <header className="detail-head">
          <h3>当前流失去向名单</h3>
          <span className="stat-label">
            {current?.数据集
              ? `${current.数据集.数据集编号} · 规则${current.数据集.规则编号}第${current.数据集.规则版本}版 · 模型${current.数据集.模型编号}第${current.数据集.模型版本}版 · 准确率${current.数据集.模型准确率 ?? '—'}`
              : '暂无当前名单'}
          </span>
        </header>
        <table className="data-table">
          <thead>
            <tr><th>用户编号</th><th>分层层级</th><th>流失概率</th><th>生成依据</th><th>规则</th><th>模型</th><th>命中原因</th></tr>
          </thead>
          <tbody>
            {(current?.成员 ?? []).map((member) => (
              <tr key={member.用户编号}>
                <td>{member.用户编号}</td>
                <td>{member.分层层级}</td>
                <td>{member.流失概率 ?? '—'}</td>
                <td>{member.生成依据}</td>
                <td>{member.规则编号}</td>
                <td>{member.模型编号}（{member.模型准确率 ?? '—'}）</td>
                <td className="wide-cell">{member.命中原因}</td>
              </tr>
            ))}
            {(current?.成员 ?? []).length === 0 && (
              <tr><td className="empty-state" colSpan={7}>当前名单为空</td></tr>
            )}
          </tbody>
        </table>

        {current?.数据集 && (
          <p className="detail-note">
            数据快照 {current.数据集.数据快照} · 生成方式 {current.数据集.生成方式} · 校验摘要 {current.数据集.校验摘要}
            {current.数据集.修复说明 ? ` · ${current.数据集.修复说明}` : ''}
          </p>
        )}

        {reconcile && reconcile.历史名单.length > 0 && (
          <>
            <h4>历史名单（保持原预警时点）</h4>
            <table className="data-table">
              <thead><tr><th>批次编号</th><th>数据集编号</th><th>名单数</th><th>预警时点</th><th>状态</th></tr></thead>
              <tbody>
                {reconcile.历史名单.map((batch) => (
                  <tr key={String(batch.批次编号)}>
                    <td>{String(batch.批次编号)}</td>
                    <td>{String(batch.数据集编号)}</td>
                    <td>{String(batch.名单数)}</td>
                    <td>{String(batch.预警时点)}</td>
                    <td>{String(batch.状态)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </section>

      {repair && (
        <section className="detail-panel">
          <header className="detail-head">
            <h3>{repair.repaired ? '已生成修复后的流失去向名单明细数据集' : '无需重复修复'}</h3>
            <span className="stat-label">
              {repair.复核 ? `按来源版本材料复核：${repair.复核.一致 ? '一致' : '不一致'} · 复核码 ${repair.复核.复核码}` : ''}
            </span>
          </header>
          {repair.数据集 && (
            <p className="detail-note">
              {repair.数据集.数据集编号} · 快照 {repair.数据集.数据快照} · {repair.数据集.名单数}人 ·
              规则{repair.数据集.规则编号}第{repair.数据集.规则版本}版 · 模型{repair.数据集.模型编号}第{repair.数据集.模型版本}版 ·
              准确率 {repair.数据集.模型准确率 ?? '—'}
            </p>
          )}
          {(repair.消除错误 ?? []).length > 0 && (
            <table className="data-table">
              <thead><tr><th>消除的错误</th><th>修复前</th><th>修复后</th><th>成员变化</th></tr></thead>
              <tbody>
                {(repair.消除错误 ?? []).map((item) => (
                  <tr key={item.类型}>
                    <td>{item.类型}</td>
                    <td>{String(item.修复前)}</td>
                    <td>{String(item.修复后)}</td>
                    <td>
                      {item.移出成员?.length ? `移出 ${item.移出成员.join('、')}` : ''}
                      {item.新增成员?.length ? ` 新增 ${item.新增成员.join('、')}` : ''}
                      {!item.移出成员?.length && !item.新增成员?.length ? '—' : ''}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {repair.复核 && repair.复核.问题.length > 0 && (
            <ul className="issue-list">{repair.复核.问题.map((item) => <li key={item}>{item}</li>)}</ul>
          )}
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条流失预警记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
