import { useRef, useState } from 'react';
import MarkdownView from '../components/MarkdownView.jsx';
import TerminalOutput from '../components/TerminalOutput.jsx';
import DatasetTable from '../components/DatasetTable.jsx';
import TrainConfirmModal from '../components/TrainConfirmModal.jsx';
import Toasts from '../components/Toasts.jsx';
import { useToasts } from '../data/useToasts.js';
import { useModelData } from '../data/useModelData.js';

const STATUS_LABEL = { idle: 'Idle', running: 'Running', done: 'Done', error: 'Error' };
const DOT_CLASSES = { idle: 'bg-slate-400', running: 'bg-accent', done: 'bg-emerald-500', error: 'bg-red-400' };

function fmtDate(iso) {
  if (!iso) return '--';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function InfoDot({ label, children }) {
  return (
    <span className="group/info relative inline-flex">
      <button
        type="button"
        className="w-5 h-5 rounded-full border border-gray-300 text-muted text-[11px] font-bold flex items-center justify-center hover:bg-gray-50 hover:text-black"
        aria-label={label}
      >
        i
      </button>
      <span className="pointer-events-none absolute left-0 top-full mt-2 w-80 rounded-lg bg-black text-white text-[11px] leading-relaxed px-3 py-2 opacity-0 group-hover/info:opacity-100 transition-opacity z-30 shadow-lg">
        {children}
      </span>
    </span>
  );
}

export default function ModelPage() {
  const { toasts, toast } = useToasts();
  const {
    dataset, datasetLoading,
    trainStatus, trainOfficialBusy, trainOfficial,
    uploads, uploading, uploadFile,
    selectedUploadId, selectedReport, selectUpload,
    trainCustom,
    anyTrainingBusy,
  } = useModelData({ toast });

  const [showConfirm, setShowConfirm] = useState(false);
  const fileInputRef = useRef(null);

  const effectiveStatus = trainStatus?.status || 'idle';
  const isCustomRunning = trainStatus?.status === 'running' && trainStatus?.kind === 'custom';
  const runningLabel = effectiveStatus === 'running'
    ? (trainStatus?.kind === 'custom' ? `custom dataset (${trainStatus.upload_id})` : 'official retrain')
    : null;
  const officialReport = trainStatus?.official_report;

  function handleFilePicked(e) {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (file) uploadFile(file);
  }

  async function handleConfirmTrain() {
    await trainOfficial();
    setShowConfirm(false);
  }

  return (
    <div className="flex-1 min-w-0">
      <div className="flex items-end justify-between flex-wrap gap-5 px-7 pt-6 pb-2">
        <div className="flex flex-col gap-2.5">
          <h1 className="text-[34px] font-extrabold m-0 tracking-tight">Model</h1>
          <div className="text-muted text-[13px]">
            models/train_tree_models.py &middot; logistic regression vs XGBoost, trained on data/train.csv + data/holdout.csv
          </div>
        </div>
        <div className="flex items-center gap-3">
          <span className="inline-flex items-center gap-1.5 text-[11px] font-bold text-muted">
            <span className={`w-1.5 h-1.5 rounded-full ${DOT_CLASSES[effectiveStatus]} ${effectiveStatus === 'running' ? 'pulse-dot relative' : ''}`} />
            {STATUS_LABEL[effectiveStatus]}{runningLabel ? ` -- ${runningLabel}` : ''}
          </span>
          <button
            type="button"
            onClick={() => setShowConfirm(true)}
            disabled={anyTrainingBusy}
            title="Retrains the official model on the original dataset"
            className="px-4 py-2 rounded-lg bg-accent text-white text-sm font-bold hover:brightness-110 disabled:opacity-50"
          >
            {trainOfficialBusy ? 'Training...' : 'Retrain model'}
          </button>
        </div>
      </div>

      <section className="px-7 pb-7 pt-2">
        <div className="bg-white border border-gray-100 rounded-2xl shadow-card p-[18px] mb-4">
          <div className="flex items-center gap-2 mb-4">
            <h2 className="text-2xl font-extrabold text-black leading-tight m-0">Dataset</h2>
            <InfoDot label="Dataset info">
              data/train.csv + data/holdout.csv &middot; the original dataset the official model trains on.
            </InfoDot>
          </div>
          {datasetLoading && !dataset ? (
            <div className="text-muted text-sm">Loading...</div>
          ) : dataset ? (
            <>
              <div className="flex flex-wrap gap-x-8 gap-y-2 mb-4">
                <div>
                  <div className="text-[10px] text-muted uppercase tracking-wide">Rows</div>
                  <div className="text-sm font-bold mt-0.5">{dataset.row_count}</div>
                </div>
                <div>
                  <div className="text-[10px] text-muted uppercase tracking-wide">Class balance ({dataset.target_column})</div>
                  <div className="text-sm font-bold mt-0.5">
                    {Object.entries(dataset.class_counts).map(([k, v]) => `${k}=${v}`).join(', ')}
                  </div>
                </div>
                <div>
                  <div className="text-[10px] text-muted uppercase tracking-wide">Features</div>
                  <div className="text-sm font-bold mt-0.5">
                    {dataset.feature_columns.numeric.length} numeric, {dataset.feature_columns.categorical.length} categorical
                  </div>
                </div>
              </div>
              <details>
                <summary className="cursor-pointer text-muted text-xs font-semibold select-none mb-2">
                  View all {dataset.row_count} rows
                </summary>
                <DatasetTable columns={dataset.columns} rows={dataset.rows} />
              </details>
            </>
          ) : (
            <div className="text-muted text-sm">Could not load dataset.</div>
          )}
        </div>

        <div className="bg-white border border-gray-100 rounded-2xl shadow-card p-[18px] mb-4">
          <div className="flex items-center gap-2 mb-4">
            <h2 className="text-2xl font-extrabold text-black leading-tight m-0">Console output</h2>
            <InfoDot label="Console output info">
              Captured stdout from the most recent training run (official retrain or a custom dataset) -- only one
              runs at a time, stored in logs/model_train_state.json so it survives a page refresh and a server
              restart.
            </InfoDot>
          </div>
          <TerminalOutput
            title={trainStatus?.kind === 'custom' ? `custom dataset training (${trainStatus.upload_id})` : 'train_tree_models.py'}
            promptLine={trainStatus?.kind === 'custom' ? `train custom dataset ${trainStatus?.upload_id || ''}` : 'python models/train_tree_models.py'}
            output={trainStatus?.output}
            error={trainStatus?.error}
            emptyMessage='No output captured yet -- click "Retrain model" above, or train a custom dataset below.'
          />
        </div>

        <div className="bg-white border border-gray-100 rounded-2xl shadow-card p-[18px] mb-4">
          <div className="flex items-center justify-between gap-2 mb-4 flex-wrap">
            <div className="flex items-center gap-2">
              <h2 className="text-2xl font-extrabold text-black leading-tight m-0">Model report</h2>
              <InfoDot label="Model report info">
                models/model_report.md &middot; LogReg vs XGBoost comparison, read straight off disk (survives a
                server restart).
              </InfoDot>
            </div>
            {officialReport?.metadata?.primary_model && (
              <div className="text-[11px] text-muted">primary: {officialReport.metadata.primary_model}</div>
            )}
          </div>
          {officialReport?.report_md ? (
            <div className="light-scroll bg-gray-50 border border-gray-100 rounded-xl px-4 py-1 max-h-[560px] overflow-auto">
              <MarkdownView markdown={officialReport.report_md} />
            </div>
          ) : (
            <div className="text-muted text-xs bg-gray-50 border border-gray-100 rounded-xl p-4">
              models/model_report.md hasn't been written yet -- retrain the model to generate it.
            </div>
          )}
        </div>

        <div className="bg-white border border-gray-100 rounded-2xl shadow-card p-[18px] mb-4">
          <div className="flex items-center gap-2 mb-1">
            <h2 className="text-2xl font-extrabold text-black leading-tight m-0">Custom dataset</h2>
            <InfoDot label="Custom dataset info">
              Upload a CSV with the same required columns as the original dataset. Stored separately under
              data/uploads/ -- never replaces data/train.csv or data/holdout.csv.
            </InfoDot>
          </div>
          <div className="text-muted text-xs mb-4">
            Upload a CSV, we'll check it has the required columns, then train the same LogReg-vs-XGBoost comparison on it.
          </div>

          <div className="flex items-center gap-3 mb-4">
            <input
              ref={fileInputRef}
              type="file"
              accept=".csv"
              onChange={handleFilePicked}
              className="hidden"
            />
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={uploading}
              className="px-4 py-2 rounded-lg border border-gray-200 text-ink text-sm font-bold hover:bg-gray-50 disabled:opacity-50"
            >
              {uploading ? 'Uploading...' : 'Upload CSV'}
            </button>
          </div>

          {uploads.length === 0 ? (
            <div className="text-muted text-xs bg-gray-50 border border-gray-100 rounded-xl p-4 mb-2">
              No datasets uploaded yet.
            </div>
          ) : (
            <div className="border border-gray-100 rounded-[10px] overflow-hidden mb-4">
              {uploads.map((u) => {
                const active = u.upload_id === selectedUploadId;
                const isThisRunning = isCustomRunning && trainStatus.upload_id === u.upload_id;
                return (
                  <div
                    key={u.upload_id}
                    className={`flex items-center justify-between gap-3 px-3.5 py-2.5 border-b border-gray-100 last:border-b-0 ${active ? 'bg-[#EEF1FF]' : ''}`}
                  >
                    <button type="button" onClick={() => selectUpload(u.upload_id)} className="flex-1 min-w-0 text-left">
                      <div className="text-sm font-semibold truncate">{u.filename}</div>
                      <div className="text-[11px] text-muted mt-0.5">
                        {u.row_count} rows &middot; uploaded {fmtDate(u.uploaded_at)}
                        {!u.valid && ' -- invalid'}
                        {u.trained && ' -- trained'}
                      </div>
                    </button>
                    <button
                      type="button"
                      onClick={() => trainCustom(u.upload_id)}
                      disabled={!u.valid || anyTrainingBusy}
                      className="px-3 py-1.5 rounded-lg bg-accent text-white text-xs font-bold hover:brightness-110 disabled:opacity-50 shrink-0"
                    >
                      {isThisRunning ? 'Training...' : (u.trained ? 'Retrain' : 'Train')}
                    </button>
                  </div>
                );
              })}
            </div>
          )}

          {selectedUploadId && (
            <div>
              {selectedReport?.report_md ? (
                <>
                  <div className="flex items-center justify-between gap-2 mb-2">
                    <div className="text-[11px] font-semibold text-muted uppercase tracking-wider">Report</div>
                    {selectedReport.metadata?.primary_model && (
                      <div className="text-[11px] text-muted">primary: {selectedReport.metadata.primary_model}</div>
                    )}
                  </div>
                  <div className="light-scroll bg-gray-50 border border-gray-100 rounded-xl px-4 py-1 max-h-[520px] overflow-auto">
                    <MarkdownView markdown={selectedReport.report_md} />
                  </div>
                </>
              ) : (
                <div className="text-muted text-xs bg-gray-50 border border-gray-100 rounded-xl p-4">
                  No report yet for this dataset -- click Train above.
                </div>
              )}
            </div>
          )}
        </div>
      </section>

      <Toasts toasts={toasts} />
      <TrainConfirmModal
        open={showConfirm}
        onClose={() => setShowConfirm(false)}
        onConfirm={handleConfirmTrain}
        busy={trainOfficialBusy}
      />
    </div>
  );
}
