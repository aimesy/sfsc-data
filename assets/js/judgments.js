const JUDGMENT_MANIFEST_URL = './data/judgments/manifest.json';
const JUDGMENT_RAW_BASE_URL = 'https://raw.githubusercontent.com/aimesy/sfsc-data/master/data/judgments/';
const EXPECTED_MANIFEST = Object.freeze({
  schema_versions: Object.freeze([1, 2, 3, 4]),
  shard_pattern: 'shards/{shard}.json',
  hash_algorithm: 'sha256_first_byte',
});
const EXPECTED_SHARD_SCHEMA_VERSION = 1;

let manifestPromise = null;
const shardPromises = new Map();

export function normalizeJudgmentCaseNumber(value) {
  return String(value ?? '').replace(/[^A-Za-z0-9]/g, '').toUpperCase();
}

export async function judgmentShardForCase(caseNumber) {
  const normalized = normalizeJudgmentCaseNumber(caseNumber);
  if (!normalized) return '';
  const bytes = new TextEncoder().encode(normalized);
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
  return digest[0].toString(16).padStart(2, '0');
}

export function escapeJudgmentHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function label(value) {
  return String(value ?? '')
    .replace(/[_/]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

const EVENT_KIND_LABELS = Object.freeze({
  amended_judgment: 'Amended judgment',
  appeal_affirmed: 'Appeal affirmed',
  appeal_dismissed: 'Appeal dismissed',
  appeal_mixed: 'Mixed appellate disposition',
  appeal_modified: 'Appeal modified',
  appeal_reinstated: 'Appeal restored to active status',
  appeal_reference: 'Appeal reference',
  appeal_reversed: 'Appeal reversed',
  default_judgment: 'Default judgment',
  'declaratory/injunctive': 'Declaratory or injunctive judgment',
  dismissal: 'Dismissal',
  dismissal_reference: 'Dismissal reference',
  criminal_acquittal: 'Criminal acquittal',
  criminal_bail_forfeiture_judgment: 'Bail forfeiture judgment',
  criminal_charge_reduction: 'Criminal charge reduction',
  criminal_competency_commitment_vacated: 'Criminal competency commitment vacated',
  criminal_competency_finding: 'Criminal competency finding',
  criminal_diversion_completed: 'Criminal diversion completed',
  criminal_diversion_denied: 'Criminal diversion denied',
  criminal_diversion_granted: 'Criminal diversion granted',
  criminal_diversion_terminated: 'Criminal diversion terminated',
  criminal_deferred_entry_of_judgment: 'Deferred entry of judgment',
  criminal_information_set_aside: 'Criminal information set aside',
  criminal_plea: 'Criminal plea',
  criminal_postconviction_relief_granted: 'Criminal postconviction relief granted',
  criminal_protective_order_modification: 'Criminal protective order modification',
  criminal_protective_order_termination: 'Criminal protective order termination',
  criminal_restitution_order: 'Criminal restitution order',
  criminal_sentence: 'Criminal sentence',
  criminal_sentence_modification: 'Criminal sentence modification',
  criminal_supervision_violation_admission: 'Criminal supervision violation admission',
  criminal_verdict: 'Criminal verdict',
  family_assignment_termination_order: 'Family assignment termination order',
  family_custody_modification: 'Child custody modification',
  family_custody_order: 'Child custody order',
  family_contempt_order: 'Family contempt order',
  family_declaratory_order: 'Family declaratory order',
  family_domestic_violence_dismissal: 'Domestic violence case dismissal',
  family_emergency_request_denied: 'Family emergency request denied',
  family_emergency_request_granted: 'Family emergency request granted',
  family_fee_cost_order: 'Family fee and cost order',
  family_guardian_ad_litem_appointment_order: 'Family guardian ad litem appointment order',
  family_judgment: 'Family judgment',
  family_judgment_enforcement_order: 'Family judgment enforcement order',
  family_name_change_order: 'Family name change order',
  family_order_reference: 'Family order reference',
  family_parentage_judgment: 'Family parentage judgment',
  family_parentage_order: 'Family parentage order',
  family_property_judgment: 'Family property judgment',
  family_property_order: 'Family property order',
  family_prior_order_ruling: 'Family prior order ruling',
  family_reserved_issues_judgment: 'Family reserved issues judgment',
  family_restraining_order: 'Family restraining order',
  family_restraining_order_denied: 'Family restraining order denied',
  family_sanctions_order: 'Family sanctions order',
  family_settlement_order: 'Family settlement order',
  family_settlement_set_aside_order: 'Family settlement set aside order',
  family_sij_findings_order: 'Family SIJ findings order',
  family_stipulated_judgment: 'Family stipulated judgment',
  family_tax_order: 'Family tax order',
  family_support_modification: 'Support modification',
  family_support_order: 'Support order',
  family_unspecified_judgment: 'Family judgment with subject not stated',
  family_unspecified_order: 'Family order (subject not stated)',
  family_vital_record_order: 'Family vital record order',
  judgment: 'Judgment',
  judgment_of_dismissal: 'Judgment of dismissal',
  judgment_reference: 'Judgment reference',
  monetary_judgment: 'Money judgment',
  name_change_decree: 'Name change decree',
  name_change_petition_denied: 'Name change petition denied',
  name_change_petition_dismissed: 'Name change petition dismissed',
  name_change_petition_stricken: 'Name change petition stricken',
  name_change_petition_withdrawn: 'Name change petition withdrawn',
  parental_obligations_judgment: 'Parental obligations judgment',
  partial_satisfaction: 'Partial satisfaction',
  possession: 'Possession judgment',
  prejudgment_writ_of_possession: 'Prejudgment writ of possession',
  probate_account_order: 'Probate account order',
  probate_administration_extension_order: 'Probate administration extension order',
  probate_administration_order: 'Probate administration order',
  probate_appointment_order: 'Probate fiduciary appointment order',
  probate_capacity_order: 'Probate capacity order',
  probate_care_authority_order: 'Probate care authority order',
  probate_case_closure_order: 'Probate case closure order',
  probate_counsel_discharge_order: 'Probate counsel discharge order',
  probate_creditor_claim_order: 'Probate creditor claim order',
  probate_decree: 'Probate decree',
  probate_discharge_order: 'Probate discharge order',
  probate_distribution_order: 'Probate distribution order',
  probate_family_allowance_order: 'Probate family allowance order',
  probate_fee_compensation_order: 'Probate fee and compensation order',
  probate_fee_waiver_order: 'Probate fee waiver order',
  probate_fiduciary_change_order: 'Probate fiduciary change order',
  probate_guardian_ad_litem_appointment_order: 'Probate guardian ad litem appointment order',
  probate_guardian_ad_litem_discharge_order: 'Probate guardian ad litem discharge order',
  probate_guardian_ad_litem_disclaimer_authority_order: 'Probate guardian ad litem disclaimer authority order',
  probate_guardian_ad_litem_order: 'Probate guardian ad litem order',
  probate_guardianship_closure: 'Probate guardianship closure',
  probate_heirship_order: 'Probate heirship order',
  probate_instructions_order: 'Probate instructions order',
  probate_letters_order: 'Probate letters order',
  probate_litigation_order: 'Probate litigation order',
  probate_marital_status_order: 'Probate marital status order',
  probate_medical_authority_order: 'Probate medical authority order',
  probate_order_modification: 'Probate order modification',
  probate_order_reference: 'Probate order reference',
  probate_parental_rights_order: 'Probate parental rights order',
  probate_property_order: 'Probate property order',
  probate_referee_appointment_order: 'Probate referee appointment order',
  probate_referee_change_order: 'Probate referee change order',
  probate_reimbursement_order: 'Probate reimbursement order',
  probate_reopening_order: 'Probate reopening order',
  probate_restraining_order: 'Probate restraining order',
  probate_sanctions_order: 'Probate sanctions order',
  probate_settlement_order: 'Probate settlement order',
  probate_sij_findings_order: 'Probate SIJ findings order',
  probate_spousal_property_order: 'Probate spousal property order',
  probate_status_order: 'Probate status order',
  probate_substituted_judgment_order: 'Probate substituted judgment order',
  probate_summary_disposition_order: 'Probate summary disposition order',
  probate_surcharge_order: 'Probate fiduciary surcharge order',
  probate_termination_order: 'Probate entity termination order',
  probate_trust_order: 'Probate trust order',
  probate_unspecified_order: 'Probate order (subject not stated)',
  probate_vital_record_order: 'Probate vital record order',
  probate_will_admission_order: 'Probate will admission order',
  remittitur: 'Remittitur',
  renewal: 'Judgment renewal',
  renewal_reference: 'Renewal reference',
  satisfaction: 'Satisfaction',
  satisfaction_reference: 'Satisfaction reference',
  satisfaction_vacatur: 'Satisfaction vacatur',
  settlement: 'Settlement reported',
  settlement_reference: 'Settlement reference',
  summary_adjudication: 'Summary adjudication',
  summary_judgment: 'Summary judgment',
  take_nothing: 'Take nothing judgment',
  unknown_end_state: 'Unresolved judgment reference',
  vacatur: 'Vacatur',
  vacatur_reference: 'Vacatur reference',
  writ_denied: 'Writ denied',
  writ_dismissed: 'Writ dismissed',
  writ_disposition: 'Writ disposition',
  writ_granted: 'Writ granted',
  writ_interim_order: 'Interim writ order',
  writ_reference: 'Writ reference',
});

const DOMAIN_LABELS = Object.freeze({
  appellate_disposition: 'Appellate disposition',
  child_custody: 'Child custody',
  child_support: 'Child support',
  civil_judgment: 'Civil judgment',
  criminal_adjudication: 'Criminal adjudication',
  criminal_competency: 'Criminal competency',
  criminal_diversion: 'Criminal diversion',
  criminal_postconviction_relief: 'Criminal postconviction relief',
  criminal_protective_order: 'Criminal protective order',
  criminal_restitution: 'Criminal restitution',
  criminal_sentence: 'Criminal sentence',
  criminal_supervision_violation: 'Criminal supervision violation',
  declaratory_injunctive: 'Declaratory or injunctive',
  dismissal: 'Dismissal',
  domestic_violence_restraint: 'Domestic violence restraint',
  family_property: 'Family property',
  family_contempt: 'Family contempt',
  family_declaratory: 'Family declaratory relief',
  family_emergency_relief: 'Family emergency relief',
  family_fees_costs: 'Family fees and costs',
  family_guardian_ad_litem_appointment: 'Family guardian ad litem appointment',
  family_judgment_enforcement: 'Family judgment enforcement',
  family_prior_order: 'Family prior order ruling',
  family_reserved_issues: 'Family reserved issues',
  family_sanctions: 'Family sanctions',
  family_settlement: 'Family settlement',
  family_sij_findings: 'Family SIJ findings',
  family_status: 'Family status',
  family_support: 'Family support',
  family_tax: 'Family tax',
  family_unspecified: 'Family matter (subject not stated)',
  family_vital_record: 'Family vital record',
  money_judgment: 'Money judgment',
  name_change: 'Name change',
  nonoperative_reference: 'Nonoperative reference',
  parentage: 'Parentage',
  parental_obligations: 'Parental obligations',
  partial_satisfaction: 'Partial satisfaction',
  possession: 'Possession',
  prejudgment_possession: 'Prejudgment possession',
  probate_account: 'Probate account',
  probate_administration: 'Probate administration',
  probate_admission: 'Probate will admission',
  probate_capacity: 'Probate capacity',
  probate_care_authority: 'Probate care authority',
  probate_counsel_discharge: 'Probate counsel discharge',
  probate_creditor_claim: 'Probate creditor claim',
  probate_discharge: 'Probate discharge',
  probate_distribution: 'Probate distribution',
  probate_family_allowance: 'Probate family allowance',
  probate_fee_compensation: 'Probate fees and compensation',
  probate_fee_waiver: 'Probate fee waiver',
  probate_fiduciary_appointment: 'Probate fiduciary appointment',
  probate_fiduciary_change: 'Probate fiduciary change',
  probate_fiduciary_surcharge: 'Probate fiduciary surcharge',
  probate_guardian_ad_litem: 'Probate guardian ad litem',
  probate_guardian_ad_litem_appointment: 'Probate guardian ad litem appointment',
  probate_guardian_ad_litem_discharge: 'Probate guardian ad litem discharge',
  probate_guardian_ad_litem_disclaimer_authority: 'Probate guardian ad litem disclaimer authority',
  probate_heirship: 'Probate heirship',
  probate_instructions: 'Probate instructions',
  probate_letters: 'Probate letters',
  probate_litigation: 'Probate litigation',
  probate_marital_status: 'Probate marital status',
  probate_medical_authority: 'Probate medical authority',
  probate_order_modification: 'Probate order modification',
  probate_parental_rights: 'Probate parental rights',
  probate_property: 'Probate property',
  probate_referee_appointment: 'Probate referee appointment',
  probate_referee_change: 'Probate referee change',
  probate_reimbursement: 'Probate reimbursement',
  probate_restraining_order: 'Probate restraining order',
  probate_sanctions: 'Probate sanctions',
  probate_settlement: 'Probate settlement',
  probate_sij_findings: 'Probate SIJ findings',
  probate_spousal_property: 'Probate spousal property',
  probate_status: 'Probate status',
  probate_substituted_judgment: 'Probate substituted judgment',
  probate_summary_disposition: 'Probate summary disposition',
  probate_termination: 'Probate termination',
  probate_trust: 'Probate trust',
  probate_unspecified: 'Probate matter (subject not stated)',
  probate_vital_record: 'Probate vital record',
  renewal: 'Renewal',
  satisfaction: 'Satisfaction',
  satisfaction_vacatur: 'Satisfaction vacatur',
  settlement: 'Settlement',
  spousal_support: 'Spousal support',
  take_nothing: 'Take nothing',
  unknown_end_state: 'Unresolved reference',
  vacatur: 'Vacatur',
  writ_petition: 'Writ petition',
});

export function eventKindLabel(value) {
  const key = String(value ?? '').trim();
  return EVENT_KIND_LABELS[key] || label(key) || 'Event';
}

function canonicalOutcomeComponents(...values) {
  for (const value of values) {
    if (!Array.isArray(value)) continue;
    const components = value.map((item) => String(item ?? '').trim()).filter(Boolean);
    if (components.length) return components;
  }
  return [];
}

function outcomeComponentsText(components) {
  return canonicalOutcomeComponents(components).map(label).join(' + ');
}

function observedDispositionLabel(kind, components, event = null) {
  const normalizedKind = normalizedState(kind || event?.disposition_domain);
  if (normalizedKind === 'dismissal') {
    const prejudice = normalizedState(event?.prejudice);
    if (prejudice === 'with') return 'Dismissal with prejudice entered';
    if (prejudice === 'without') return 'Dismissal without prejudice entered';
    return 'Dismissal entered';
  }
  const observed = outcomeComponentsText(components);
  if (!observed) return eventKindLabel(kind);
  return observed.charAt(0).toUpperCase() + observed.slice(1);
}

const TERMINATED_CLOSURE_STATES = new Set(['terminated', 'closed', 'whole_case_terminated']);
const OPEN_CLOSURE_STATES = new Set(['open', 'active', 'pending', 'affirmatively_open']);

function normalizedState(value) {
  return String(value ?? '').trim().toLowerCase().replace(/[ -]+/g, '_');
}

function canonicalSummary(record) {
  if (!record || typeof record !== 'object') return {};
  const summary = record.summary && typeof record.summary === 'object'
    ? record.summary
    : record.operative_summary && typeof record.operative_summary === 'object'
      ? record.operative_summary
      : {};
  return summary;
}

function canonicalEvents(record) {
  return Array.isArray(record?.events) ? record.events.filter((event) => event && typeof event === 'object') : [];
}

function canonicalEventForHash(events, hash) {
  if (!hash) return null;
  return events.find((event) => [event.entry_hash, event.source_row_hash, event.source_evidence_hash].includes(hash)) || null;
}

function canonicalCurrentEvent(summary, events) {
  for (const key of ['latest_dispositive_event_hash', 'selected_event_hash', 'current_judgment_event_hash', 'actual_judgment_event_hash']) {
    const found = canonicalEventForHash(events, summary[key]);
    if (found) return found;
  }
  const operative = events.filter((event) => ['operative', 'superseding'].includes(event.status));
  return operative[operative.length - 1] || null;
}

/**
 * Consumer projection of the canonical outcome record.
 *
 * This function never classifies raw docket text.  It also never treats a
 * disposition event as proof that the whole case closed unless the canonical
 * summary explicitly supplies `case_closure_status`.
 */
export function canonicalCaseStatusSummary(record, baseSummary = null) {
  const base = baseSummary && typeof baseSummary === 'object' ? baseSummary : {};
  const summary = canonicalSummary(record);
  const events = canonicalEvents(record);
  const operative = events.filter((event) => ['operative', 'superseding'].includes(event.status));
  const current = canonicalCurrentEvent(summary, events);
  const groups = Array.isArray(summary.disposition_groups) ? summary.disposition_groups : [];
  const domains = Array.isArray(summary.disposition_domains)
    ? summary.disposition_domains.filter(Boolean)
    : [...new Set(operative.flatMap((event) => event.disposition_domains || [event.disposition_domain]).filter(Boolean))];
  const closureStatus = String(summary.case_closure_status || record?.case_closure_status || '').trim();
  const closureEffect = String(summary.case_closure_effect || record?.case_closure_effect || '').trim();
  const closureBasis = String(summary.case_closure_basis || record?.case_closure_basis || '').trim();
  const normalizedClosureStatus = normalizedState(closureStatus);
  const vacated = Boolean(summary.judgment_is_vacated)
    || groups.some((group) => ['vacated', 'set_aside'].includes(normalizedState(group?.current_effect || group?.current_state)));
  const wholeCaseTerminated = TERMINATED_CLOSURE_STATES.has(normalizedClosureStatus) && !vacated;
  const canonicalOpen = OPEN_CLOSURE_STATES.has(normalizedClosureStatus);
  const explicitOpen = base.case_status === 'affirmatively_open' && base.affirmatively_open;
  const hasEvidence = operative.length > 0 || groups.length > 0;
  const currentKind = String(current?.event_kind || summary.latest_dispositive_event_kind || summary.selected_event_kind || '').trim();
  const currentComponents = canonicalOutcomeComponents(
    current?.outcome_components,
    summary.latest_dispositive_outcome_components,
  );
  const currentDate = String(current?.entry_date || summary.latest_dispositive_event_date || '').trim();
  const currentDomain = String(current?.disposition_domain || '').trim();
  const hasCurrentFinal = !vacated && (
    groups.some((group) => group?.is_current_final === true || (
      group?.is_current_final === undefined
      && group?.is_final
      && !['vacated', 'superseded', 'reversed', 'set_aside'].includes(normalizedState(group?.current_effect || group?.current_state))
    ))
    || operative.some((event) => event.is_final_disposition
      && !['vacated', 'superseded', 'reversed', 'set_aside'].includes(normalizedState(event.current_effect))
      && !['vacatur', 'renewal', 'satisfaction', 'partial_satisfaction'].includes(event.event_kind))
  );
  const satisfied = Boolean(summary.judgment_is_satisfied);
  let caseStatus = 'unknown';
  let statusLabel = 'Unknown / no final disposition detected';
  if (base.no_data) {
    caseStatus = 'unavailable';
    statusLabel = base.status_label || 'No data';
  } else if (satisfied) {
    caseStatus = 'judgment_satisfied';
    statusLabel = 'Judgment satisfied';
  } else if (wholeCaseTerminated) {
    caseStatus = 'case_terminated';
    statusLabel = 'Case terminated by canonical disposition evidence';
  } else if (vacated && hasEvidence) {
    caseStatus = 'disposition_vacated';
    statusLabel = 'Prior disposition vacated or set aside';
  } else if (hasEvidence) {
    caseStatus = 'disposition_evidence';
    const observed = observedDispositionLabel(currentKind || currentDomain, currentComponents, current);
    statusLabel = normalizedState(currentKind || currentDomain) === 'dismissal'
      ? observed
      : observed ? `Disposition evidence: ${observed}` : 'Disposition evidence recorded';
  } else if (canonicalOpen || explicitOpen) {
    caseStatus = 'affirmatively_open';
    statusLabel = base.status_label || `Court status: ${label(closureStatus)}`;
  }
  const sourceText = String(current?.source_text || '').trim();
  const finalityLabel = base.no_data
    ? 'No data'
    : wholeCaseTerminated
      ? `Whole-case termination${currentDate ? ` on ${currentDate}` : ''}${closureBasis ? ` (${closureBasis})` : ''}`
      : hasEvidence
        ? `${observedDispositionLabel(currentKind || currentDomain, currentComponents, current)}${currentDate ? ` on ${currentDate}` : ''}; whole-case closure not established`
        : 'Unknown / no final disposition detected';
  return {
    ...base,
    status_domain: String(summary.case_model || record?.case_model || base.status_domain || 'unknown'),
    case_status: caseStatus,
    status_label: statusLabel,
    status_label_html: '',
    status_detail: sourceText || (hasEvidence ? 'Open the canonical outcome evidence below for the exact retained source row.' : base.status_detail || ''),
    no_data: Boolean(base.no_data),
    has_disposition_evidence: hasEvidence,
    has_final_disposition: hasCurrentFinal,
    has_current_final_disposition: hasCurrentFinal,
    whole_case_terminated: wholeCaseTerminated,
    affirmatively_open: Boolean(canonicalOpen || explicitOpen),
    final_disposition_type: currentKind || currentDomain,
    final_disposition_date: currentDate,
    finality_label: finalityLabel,
    judgment_entered: Boolean(summary.current_judgment_event_hash || summary.actual_judgment_event_hash) && !vacated,
    judgment_is_vacated: vacated,
    satisfied,
    disposition_domains: domains,
    disposition_groups: groups,
    current_event_hash: String(current?.entry_hash || summary.latest_dispositive_event_hash || ''),
    current_event_kind: currentKind,
    outcome_components: currentComponents,
    current_disposition_domain: currentDomain,
    case_closure_status: closureStatus || 'unknown',
    case_closure_effect: closureEffect || (wholeCaseTerminated ? 'closes_case' : 'unknown'),
    case_closure_basis: closureBasis,
    outcome_rule_id: String(record?.rule_id || ''),
    outcome_rule_version: String(record?.rule_version || ''),
    appeal_status: 'unknown_not_computed_from_outcome_evidence',
    appeal_label: 'Not computed from canonical outcome evidence',
    scan_warning: base.scan_warning || null,
    signals: {
      ...(base.signals || {}),
      canonicalDisposition: current,
      canonicalEvents: operative,
    },
  };
}

export function domainLabel(value) {
  const key = String(value ?? '').trim();
  return DOMAIN_LABELS[key] || label(key) || 'Domain not stated';
}

function money(value) {
  const raw = String(value ?? '').trim().replace(/,/g, '');
  const match = raw.match(/^(-?)(\d+)(\.\d+)?$/);
  if (!match) return raw || 'not stated';
  const whole = match[2].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  return `${match[1]}$${whole}${match[3] || ''}`;
}

function confidence(value) {
  if (value === null || value === undefined || value === '') return 'not stated';
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  return `${(numeric * 100).toFixed(numeric === 1 ? 0 : 1)}%`;
}

function kv(labelText, valueHtml) {
  return `<div class="cs-kv"><div class="cs-kv-label">${escapeJudgmentHtml(labelText)}</div><div class="cs-kv-val">${valueHtml}</div></div>`;
}

function badge(value, extra = '') {
  return `<span class="cs-badge${extra ? ` ${extra}` : ''}">${escapeJudgmentHtml(label(value) || 'not stated')}</span>`;
}

function eventForHash(events, hash) {
  if (!hash) return null;
  return events.find((event) =>
    event && (event.entry_hash === hash || event.source_row_hash === hash || event.source_evidence_hash === hash)
  ) || null;
}

function eventReviewText(event) {
  const reasons = Array.isArray(event?.review_reasons) ? event.review_reasons.filter(Boolean) : [];
  return reasons.length ? reasons.map(label).join('; ') : 'none recorded';
}

const EVENT_AMOUNT_FIELDS = Object.freeze([
  ['total_amount', 'total amount', 'total'],
  ['principal_amount', 'principal amount', 'principal'],
  ['interest_amount', 'interest amount', 'interest'],
  ['costs_amount', 'costs amount', 'costs'],
  ['fees_amount', 'fees amount', 'fees'],
  ['reimbursement_amount', 'reimbursement amount', 'reimbursement'],
  ['sanctions_amount', 'sanctions amount', 'sanctions'],
  ['damages_amount', 'damages amount', 'damages'],
  ['restoration_amount', 'restoration amount', 'restoration'],
  ['satisfied_amount', 'satisfied amount', 'satisfied'],
]);

function eventAmountValue(row, field, role) {
  if (row[field] !== null && row[field] !== undefined && String(row[field]).trim() !== '') {
    return row[field];
  }
  const mentions = Array.isArray(row.money_mentions) ? row.money_mentions : [];
  const mention = mentions.find((item) => Array.isArray(item?.roles) && item.roles.includes(role));
  return mention?.amount ?? null;
}

function eventPrimaryAmount(row) {
  for (const [field, _labelText, role] of EVENT_AMOUNT_FIELDS) {
    const value = eventAmountValue(row, field, role);
    if (value !== null && value !== undefined && String(value).trim() !== '') return value;
  }
  return null;
}

function renderEventAmounts(row) {
  const values = EVENT_AMOUNT_FIELDS.map(([field, labelText, role]) => {
    const value = eventAmountValue(row, field, role);
    return value === null || value === undefined || String(value).trim() === ''
      ? ''
      : kv(labelText, `<span class="mono">${escapeJudgmentHtml(money(value))}</span>`);
  }).join('');
  const mentions = (Array.isArray(row.money_mentions) ? row.money_mentions : []).map((item) => {
    const roles = Array.isArray(item?.roles) ? item.roles.map(label).filter(Boolean).join(', ') : '';
    const raw = String(item?.raw ?? '');
    return raw ? `${raw}${roles ? ` (${roles})` : ''}` : '';
  }).filter(Boolean);
  return values + (mentions.length
    ? kv('money evidence', `<span class="mono">${escapeJudgmentHtml(mentions.join('; '))}</span>`)
    : '');
}

function renderDispositionGroups(summary) {
  const groups = Array.isArray(summary?.disposition_groups) ? summary.disposition_groups : [];
  if (!groups.length) return '';
  const rows = groups.map((group, index) => {
    const row = group && typeof group === 'object' ? group : {};
    const domain = domainLabel(row.disposition_domain);
    const scope = label(row.disposition_scope) || 'scope not stated';
    const latestKind = eventKindLabel(row.latest_event_kind);
    const components = outcomeComponentsText(row.outcome_components);
    const latestDate = String(row.latest_event_date ?? '');
    const amount = row.current_amount == null ? '' : money(row.current_amount);
    return `<details class="cs-packet-member" data-judgment-disposition="${escapeJudgmentHtml(index)}">`
      + `<summary><span>${escapeJudgmentHtml(latestDate || 'no date')}</span><span class="cs-packet-code">${escapeJudgmentHtml(domain)}</span><span class="cs-packet-member-title">${escapeJudgmentHtml(latestKind)}</span><span class="mono">${escapeJudgmentHtml(amount)}</span></summary>`
      + `<div class="cs-packet-member-body">`
      + `<div class="cs-kv-grid">`
      + kv('case model', escapeJudgmentHtml(label(row.case_model) || 'not stated'))
      + kv('scope', escapeJudgmentHtml(scope))
      + (components ? kv('observed outcome components', escapeJudgmentHtml(components)) : '')
      + kv('unit key', `<span class="mono">${escapeJudgmentHtml(row.disposition_unit_key || 'not recorded')}</span>`)
      + kv('latest event hash', `<span class="mono">${escapeJudgmentHtml(row.latest_event_hash || 'not recorded')}</span>`)
      + kv('events', escapeJudgmentHtml(String(row.event_count ?? 0)))
      + kv('final events', escapeJudgmentHtml(String(row.final_event_count ?? 0)))
      + kv('modifiable', stateValue(Boolean(row.is_modifiable), 'modifiable', 'not marked modifiable'))
      + kv('modifications', stateValue(Boolean(row.has_modifications), 'modification recorded', 'none recorded'))
      + kv('review', stateValue(Boolean(row.review_required), 'review required', 'no review flags'))
      + `</div></div></details>`;
  }).join('');
  return `<div class="cs-section-note">${escapeJudgmentHtml(groups.length)} disposition track${groups.length === 1 ? '' : 's'} from operative source events.</div>`
    + `<div class="cs-packet-members" data-judgment-disposition-groups>${rows}</div>`;
}

function renderEvent(event, index) {
  const row = event && typeof event === 'object' ? event : {};
  const status = label(row.status) || 'unknown';
  const kind = eventKindLabel(row.event_kind);
  const components = outcomeComponentsText(row.outcome_components);
  const domain = domainLabel(row.disposition_domain);
  const sourceText = String(row.source_text ?? '');
  const entryDate = String(row.entry_date ?? '');
  const docId = String(row.entry_doc_id ?? '');
  const sourcePath = String(row.source_path ?? '');
  const evidenceHash = String(row.source_evidence_hash ?? '');
  const rowHash = String(row.source_row_hash ?? row.entry_hash ?? '');
  const rowAmount = eventPrimaryAmount(row);
  const amount = rowAmount == null ? '' : ` ${money(rowAmount)}`;
  const review = eventReviewText(row);
  return `<details class="cs-packet-member" data-judgment-event="${escapeJudgmentHtml(index)}">`
    + `<summary><span>${escapeJudgmentHtml(entryDate || 'no date')}</span><span class="cs-packet-code">${escapeJudgmentHtml(status)}</span><span class="cs-packet-member-title">${escapeJudgmentHtml(kind)}</span><span>${escapeJudgmentHtml(domain)}</span><span class="mono">${escapeJudgmentHtml(amount.trim())}</span></summary>`
    + `<div class="cs-packet-member-body" data-judgment-evidence="${escapeJudgmentHtml(evidenceHash || rowHash)}">`
    + `<div class="cs-kv-grid">`
    + kv('case model', escapeJudgmentHtml(label(row.case_model) || 'not stated'))
    + kv('domain', escapeJudgmentHtml(domain))
    + kv('all domains', escapeJudgmentHtml((Array.isArray(row.disposition_domains) ? row.disposition_domains : []).map(domainLabel).join('; ') || domain))
    + kv('scope', escapeJudgmentHtml(label(row.disposition_scope) || 'not stated'))
    + (components ? kv('observed outcome components', escapeJudgmentHtml(components)) : '')
    + (row.prejudice ? kv('dismissal prejudice', escapeJudgmentHtml(label(row.prejudice))) : '')
    + (row.voluntariness ? kv('dismissal voluntariness', escapeJudgmentHtml(label(row.voluntariness))) : '')
    + (Number(row.source_effect_count || 1) > 1
      ? kv('source row effect', escapeJudgmentHtml(`${Number(row.source_effect_index || 0) + 1} of ${Number(row.source_effect_count)}`))
      : '')
    + renderEventAmounts(row)
    + kv('unit key', `<span class="mono">${escapeJudgmentHtml(row.disposition_unit_key || 'not recorded')}</span>`)
    + kv('modification', stateValue(Boolean(row.is_modification), 'modification', 'not marked modification'))
    + kv('date', `<span class="mono">${escapeJudgmentHtml(entryDate || 'not recorded')}</span>`)
    + kv('document', `<span class="mono">${escapeJudgmentHtml(docId || 'not recorded')}</span>`)
    + kv('source path', `<span class="mono">${escapeJudgmentHtml(sourcePath || 'not recorded')}</span>`)
    + kv('evidence hash', `<span class="mono">${escapeJudgmentHtml(evidenceHash || 'not recorded')}</span>`)
    + kv('row hash', `<span class="mono">${escapeJudgmentHtml(rowHash || 'not recorded')}</span>`)
    + kv('confidence', escapeJudgmentHtml(confidence(row.confidence)))
    + kv('review', escapeJudgmentHtml(review))
    + kv('rule', `<span class="mono">${escapeJudgmentHtml(row.rule_id || 'not recorded')}</span>`)
    + `</div>`
    + `<div><b>Exact source text</b><pre class="mono" data-judgment-source-text>${escapeJudgmentHtml(sourceText)}</pre></div>`
    + `</div></details>`;
}

function stateValue(active, activeLabel, inactiveLabel) {
  return badge(active ? activeLabel : inactiveLabel, active ? '' : 'cs-src');
}

function renderReview(summary, events) {
  const reasons = [];
  if (summary.review_required) reasons.push('summary conflict');
  const conflicts = Array.isArray(summary.conflicts) ? summary.conflicts : [];
  conflicts.forEach((item) => {
    const reason = item && typeof item === 'object' ? item.reason : item;
    if (reason) reasons.push(label(reason));
  });
  events.forEach((event) => {
    (Array.isArray(event?.review_reasons) ? event.review_reasons : []).forEach((reason) => {
      if (reason) reasons.push(label(reason));
    });
  });
  const unique = [...new Set(reasons)];
  return unique.length ? `review required: ${unique.join('; ')}` : 'no review flags';
}

function hasValue(value) {
  return value !== null && value !== undefined && String(value).trim() !== '';
}

function firstPresent(...values) {
  return values.find(hasValue) ?? null;
}

function amountValue(summary) {
  return firstPresent(
    summary.recorded_judgment_amount,
    summary.latest_renewal_total_amount,
    summary.actual_judgment_total_amount,
    summary.original_judgment_total_amount
  );
}

function percentText(value) {
  if (!hasValue(value)) return '';
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  const pct = numeric <= 1 ? numeric * 100 : numeric;
  return `${pct.toFixed(Math.abs(pct - Math.round(pct)) < 0.05 ? 0 : 1)}%`;
}

function satisfactionLabel(summary) {
  const explicit = String(summary?.satisfaction_status_label || '').trim();
  if (explicit) {
    return explicit.toLowerCase() === 'unsatisfied'
      ? 'no current full satisfaction recorded'
      : explicit;
  }
  if (summary?.judgment_is_satisfied) return 'completely satisfied';
  const satisfiedAmount = summary?.satisfaction_amount;
  const basisAmount = summary?.satisfaction_basis_amount ?? amountValue(summary);
  const pct = percentText(summary?.satisfaction_percent);
  if (hasValue(satisfiedAmount) && hasValue(basisAmount)) {
    return `${pct || 'partially'} satisfied (${money(satisfiedAmount)}/${money(basisAmount)})`;
  }
  if (hasValue(satisfiedAmount)) return `partially satisfied (${money(satisfiedAmount)} recorded)`;
  return 'no current full satisfaction recorded';
}

const SATISFACTION_NOT_APPLICABLE_KINDS = new Set([
  'declaratory/injunctive',
  'name_change_decree',
  'take_nothing',
]);
const FINANCIAL_JUDGMENT_KINDS = new Set([
  'amended_judgment',
  'default_judgment',
  'judgment',
  'monetary_judgment',
  'possession',
]);
const FALLBACK_SATISFACTION_APPLICABLE_KINDS = new Set([
  'amended_judgment',
  'default_judgment',
  'dismissal',
  'judgment',
  'judgment_of_dismissal',
  'monetary_judgment',
  'possession',
  'stipulated_judgment',
]);

function capitalizeSentence(value) {
  const text = String(value || '').trim().replace(/[.]+$/, '');
  return text ? `${text.charAt(0).toUpperCase()}${text.slice(1)}.` : '';
}

function fallbackSatisfactionText(summary) {
  if (!fallbackHasFinalSignal(summary)) return '';
  const kind = String(summary.final_disposition_type || '').trim().toLowerCase().replace(/[ -]+/g, '_');
  if (summary.satisfaction_date || summary.satisfied) return 'Satisfied.';
  if (summary.name_change_decree_entered || !FALLBACK_SATISFACTION_APPLICABLE_KINDS.has(kind)) return '';
  return 'No current full satisfaction recorded.';
}

function postureSatisfactionText(model, fallbackSummary) {
  if (model.referenceOnly) return fallbackSatisfactionText(fallbackSummary);
  if (!model.financialJudgmentApplicable) return '';
  if (SATISFACTION_NOT_APPLICABLE_KINDS.has(model.kind)) return '';
  const state = String(model.summary?.satisfaction_state || '').trim();
  const explicit = String(model.summary?.satisfaction_status_label || '').trim();
  if (!state && !explicit) return '';
  return capitalizeSentence(explicit || satisfactionLabel(model.summary));
}

function judgmentDescriptionModel(record) {
  const source = record && typeof record === 'object' ? record : {};
  const hasSummary = Boolean(source.summary && typeof source.summary === 'object');
  const summary = hasSummary ? source.summary : {};
  const events = Array.isArray(source.events) ? source.events : [];
  if (!events.length && !Object.keys(summary).length) {
    return {
      empty: true,
      summary,
      events,
      nature: 'No extracted judgment/end-state events are recorded for this case.',
      amountDescription: 'Final amount: not stated.',
      satisfactionText: 'not applicable',
      effectDescription: '',
      sourceDescription: 'No judgment source rows were found in the generated judgment shard.',
      reviewText: 'no review flags',
      dispositionDomains: '',
      recordedAmount: null,
    };
  }

  const operativeEvent = events.find((event) => ['operative', 'superseding'].includes(event?.status));
  const currentEvent = eventForHash(events, summary.actual_judgment_event_hash)
    || eventForHash(events, summary.current_judgment_event_hash)
    || eventForHash(events, summary.selected_event_hash)
    || operativeEvent
    || events[0]
    || {};
  const hasOperativeJudgment = Boolean(
    summary.actual_judgment_event_hash
    || summary.current_judgment_event_hash
    || summary.actual_judgment_kind
    || summary.current_judgment_total_amount
    || summary.original_judgment_total_amount
    || summary.recorded_judgment_amount
    || Number(summary.final_disposition_count || 0) > 0
    || operativeEvent
  );
  const referenceOnly = events.length > 0 && !hasOperativeJudgment;
  const finalEvent = eventForHash(events, summary.latest_dispositive_event_hash)
    || eventForHash(events, summary.selected_event_hash)
    || currentEvent;
  const kind = referenceOnly ? 'judgment_reference' : (hasSummary
    ? (summary.actual_judgment_kind || currentEvent.event_kind || summary.selected_event_kind || 'judgment')
    : (currentEvent.event_kind || 'judgment_reference'));
  const finalKind = referenceOnly ? 'judgment_reference' : (summary.latest_dispositive_event_kind || finalEvent.event_kind || kind);
  const finalComponents = canonicalOutcomeComponents(
    summary.latest_dispositive_outcome_components,
    finalEvent.outcome_components,
  );
  const finalDate = String(summary.latest_dispositive_event_date || finalEvent.entry_date || '');
  const currentDate = String(currentEvent.entry_date || '');
  const finalLabel = observedDispositionLabel(finalKind, finalComponents, finalEvent);
  const judgmentLabel = eventKindLabel(kind);
  const dispositionDomains = Array.isArray(summary.disposition_domains)
    ? summary.disposition_domains.map(domainLabel).filter(Boolean).join('; ')
    : '';
  const recordedAmount = amountValue(summary);
  const financialJudgmentApplicable = !SATISFACTION_NOT_APPLICABLE_KINDS.has(kind) && (
    FINANCIAL_JUDGMENT_KINDS.has(kind)
    || hasValue(recordedAmount)
    || hasValue(summary.satisfaction_state)
    || hasValue(summary.satisfaction_status_label)
    || Boolean(summary.judgment_is_satisfied)
  );
  const takeNothing = kind === 'take_nothing' || finalKind === 'take_nothing'
    || (Array.isArray(summary.disposition_domains) && summary.disposition_domains.includes('take_nothing'));
  let amountDescription;
  if (takeNothing) {
    amountDescription = 'Final amount: take nothing.';
  } else if (hasValue(recordedAmount)) {
    amountDescription = `Final amount: ${money(recordedAmount)} recorded in the judgment/renewal source.`;
  } else if (financialJudgmentApplicable) {
    amountDescription = 'Final amount: not stated in extracted judgment rows.';
  } else {
    amountDescription = '';
  }
  const satisfactionText = financialJudgmentApplicable ? satisfactionLabel(summary) : '';
  const effects = [];
  if (summary.judgment_is_vacated) effects.push('vacated');
  if (summary.judgment_has_party_limited_satisfaction) effects.push('party-limited satisfaction recorded');
  if (summary.judgment_has_party_limited_vacatur) effects.push('party-limited vacatur recorded');
  const reviewText = renderReview(summary, events);
  const finalDateText = finalDate ? ` on ${finalDate}` : '';
  const currentDateText = currentDate && currentDate !== finalDate ? ` on ${currentDate}` : '';
  const closureStatus = normalizedState(summary.case_closure_status);
  const scope = normalizedState(finalEvent.disposition_scope);
  const dispositionHeading = TERMINATED_CLOSURE_STATES.has(closureStatus)
    ? 'Whole-case disposition'
    : ({
      issue: 'Issue order',
      petition: 'Petition disposition',
      claim: 'Claim disposition',
      count: 'Count disposition',
      party: 'Party disposition',
      case: 'Case-level disposition evidence',
    }[scope] || 'Disposition evidence');
  const nature = referenceOnly
    ? 'no operative judgment was extracted; reference and rejected judgment source rows are retained for review.'
    : [
      `${dispositionHeading}: ${finalLabel}${finalDateText}.`,
      financialJudgmentApplicable && judgmentLabel && judgmentLabel !== finalLabel
        ? `Operative judgment: ${judgmentLabel}${currentDateText}.`
        : '',
      dispositionDomains ? `Tracks: ${dispositionDomains}.` : '',
    ].filter(Boolean).join(' ');
  const sourceDescription = [
    `${events.length} judgment/end-state source event${events.length === 1 ? '' : 's'}`,
    `${summary.disposition_group_count ?? 0} disposition track${Number(summary.disposition_group_count ?? 0) === 1 ? '' : 's'}`,
    `${summary.final_disposition_count ?? 0} final event${Number(summary.final_disposition_count ?? 0) === 1 ? '' : 's'}`,
  ].join('; ') + '.';
  return {
    empty: false,
    summary,
    events,
    currentEvent,
    finalEvent,
    referenceOnly,
    kind,
    finalKind,
    finalComponents,
    finalDate,
    currentDate,
    nature,
    amountDescription,
    satisfactionText,
    financialJudgmentApplicable,
    effectDescription: effects.join('; '),
    sourceDescription,
    reviewText,
    dispositionDomains,
    recordedAmount,
    prevailing: currentEvent.prevailing_party_text || 'not stated',
    liable: currentEvent.liable_party_text || 'not stated',
  };
}

export function renderJudgmentSummary(record, mode = 'inline') {
  const model = judgmentDescriptionModel(record);
  if (model.empty) {
    return `<div class="cs-judgment-summary is-empty" data-judgment-state="no-case">${escapeJudgmentHtml(model.nature)}</div>`;
  }
  const compact = mode === 'table';
  const reviewBadge = model.reviewText.startsWith('review required') ? badge('review required') : '';
  const effect = model.effectDescription ? `<div>${escapeJudgmentHtml(model.effectDescription)}.</div>` : '';
  return `<div class="cs-judgment-summary${compact ? ' is-table' : ''}" data-judgment-state="ready">`
    + `<div class="cs-judgment-nature">${escapeJudgmentHtml(model.nature)}</div>`
    + (model.amountDescription ? `<div class="cs-judgment-amount">${escapeJudgmentHtml(model.amountDescription)}</div>` : '')
    + (model.satisfactionText ? `<div class="cs-judgment-satisfaction">Satisfaction: ${escapeJudgmentHtml(model.satisfactionText)}.</div>` : '')
    + effect
    + (compact ? '' : `<div class="cs-judgment-source">${escapeJudgmentHtml(model.sourceDescription)} ${reviewBadge}</div>`)
    + `</div>`;
}

export function renderJudgmentPosture(record, fallbackSummary = null) {
  const model = judgmentDescriptionModel(record);
  const satisfaction = postureSatisfactionText(model, fallbackSummary);
  const satisfactionHtml = satisfaction
    ? `<div class="cs-judgment-satisfaction">${escapeJudgmentHtml(satisfaction)}</div>`
    : '';
  return `<div class="cs-judgment-posture">`
    + satisfactionHtml
    + `<details class="cs-judgment-inline-details">`
    + `<summary>Judgment details</summary>`
    + renderJudgmentRecord(record)
    + `</details>`
    + `</div>`;
}

export function renderJudgmentRecord(record) {
  const model = judgmentDescriptionModel(record);
  const summary = model.summary;
  const events = model.events;
  if (model.empty) {
    return '<div class="cs-section-note" data-judgment-state="no-case">No extracted judgment events are recorded for this case.</div>';
  }
  const currentEvent = model.currentEvent || {};
  const kind = model.kind;
  const reviewText = model.reviewText;
  const reviewBadge = reviewText.startsWith('review required') ? badge('review required') : badge('no review flags', 'cs-src');
  const takeNothing = kind === 'take_nothing' ? badge('take nothing') : badge(eventKindLabel(kind));
  const eventRows = events.map(renderEvent).join('');
  const dispositionGroups = renderDispositionGroups(summary);
  const recordedAmount = model.recordedAmount;

  const referenceNote = model.referenceOnly
    ? `<div class="cs-section-note">${escapeJudgmentHtml(model.nature)}</div>`
    : '';
  return `<div data-judgment-state="ready">`
    + referenceNote
    + `<div class="cs-kv-grid">`
    + kv('case model', escapeJudgmentHtml(label(summary.case_model) || label(currentEvent.case_model) || 'not stated'))
    + kv('domains', escapeJudgmentHtml(model.dispositionDomains || label(currentEvent.disposition_domain) || 'not stated'))
    + kv('tracks', escapeJudgmentHtml(String(summary.disposition_group_count ?? 0)))
    + kv('final events', escapeJudgmentHtml(String(summary.final_disposition_count ?? 0)))
    + kv('kind', takeNothing)
    + (model.finalComponents.length ? kv('observed outcome components', escapeJudgmentHtml(outcomeComponentsText(model.finalComponents))) : '')
    + (model.financialJudgmentApplicable ? kv('satisfaction', escapeJudgmentHtml(model.satisfactionText)) : '')
    + kv('vacated', stateValue(summary.judgment_is_vacated, 'vacated', 'not vacated'))
    + kv('party-limited effects', stateValue(
      Boolean(summary.judgment_has_party_limited_satisfaction || summary.judgment_has_party_limited_vacatur),
      'party-limited effect recorded',
      'none recorded'
    ))
    + (model.financialJudgmentApplicable
      ? kv('actual judgment amount', `<span class="mono">${escapeJudgmentHtml(money(summary.actual_judgment_total_amount ?? summary.original_judgment_total_amount))}</span>`)
        + kv('recorded judgment amount', `<span class="mono">${escapeJudgmentHtml(money(recordedAmount))}</span>`)
        + kv('latest renewal', `<span class="mono">${escapeJudgmentHtml(money(summary.latest_renewal_total_amount))}</span>`)
      : '')
    + kv('confidence', escapeJudgmentHtml(confidence(currentEvent.confidence)))
    + kv('prevailing party', escapeJudgmentHtml(model.prevailing))
    + kv('liable party', escapeJudgmentHtml(model.liable))
    + kv('review', `${reviewBadge} ${escapeJudgmentHtml(reviewText)}`)
    + `</div>`
    + dispositionGroups
    + `<div class="cs-section-note">${escapeJudgmentHtml(events.length)} judgment/end-state source event${events.length === 1 ? '' : 's'}. Open every row for exact source evidence; reference and rejected rows are retained.</div>`
    + `<div class="cs-packet-members" data-judgment-events>${eventRows}</div>`
    + `</div>`;
}

export function judgmentPanelShell(caseNumber, view = 'panel') {
  const normalized = normalizeJudgmentCaseNumber(caseNumber);
  return `<div data-judgment-view="${escapeJudgmentHtml(view)}" data-judgment-case="${escapeJudgmentHtml(normalized)}"><div class="cs-section-note" data-judgment-state="loading">Loading judgment data...</div></div>`;
}

function compactJudgmentNoteHtml(message, state = 'unavailable') {
  return `<div class="cs-judgment-summary is-empty" data-judgment-state="${escapeJudgmentHtml(state)}">${escapeJudgmentHtml(message)}</div>`;
}

function unavailableHtml(message, view = 'panel') {
  if (view === 'table-summary' || view === 'posture') return compactJudgmentNoteHtml(message, 'unavailable');
  return `<div class="cs-section-note" data-judgment-state="unavailable">${escapeJudgmentHtml(message)}</div>`;
}

function noCaseHtml(view = 'panel') {
  const message = 'No extracted judgment/end-state events are recorded for this case.';
  if (view === 'table-summary' || view === 'posture') return compactJudgmentNoteHtml(message, 'no-case');
  return `<div class="cs-section-note" data-judgment-state="no-case">${escapeJudgmentHtml(message)}</div>`;
}

function fallbackText(value) {
  return String(value ?? '').replace(/\s+/g, ' ').trim();
}

function fallbackHasFinalSignal(summary) {
  return Boolean(summary && typeof summary === 'object' && summary.has_final_disposition && !summary.no_data);
}

function fallbackAmountDescription(summary) {
  if (!fallbackHasFinalSignal(summary)) return 'Final amount: not stated.';
  if (summary.dismissal_entered && !summary.judgment_entered) {
    return 'Final amount: no money judgment amount detected in the extracted case-status signal.';
  }
  if (summary.satisfied && !summary.judgment_entered) {
    return 'Final amount: not stated in the extracted satisfaction signal.';
  }
  return 'Final amount: not stated in extracted ROA/document metadata.';
}

function fallbackSourceSnippet(summary) {
  const signals = summary?.signals || {};
  return fallbackText(
    summary?.status_detail
    || signals.judgment?.snippet
    || signals.dismissal?.snippet
    || signals.satisfaction?.snippet
    || signals.remittitur?.snippet
    || signals.appealNotice?.snippet
    || ''
  );
}

function fallbackJudgmentHtml(summary, view = 'panel') {
  if (!fallbackHasFinalSignal(summary)) return '';
  const finality = fallbackText(summary.finality_label || summary.status_label || 'Final disposition detected');
  const status = fallbackText(summary.status_label || '');
  const date = fallbackText(summary.final_disposition_date || summary.judgment_date || summary.dismissal_date || summary.satisfaction_date || '');
  const source = fallbackSourceSnippet(summary);
  const appeal = fallbackText(summary.appeal_label || '');
  const review = summary.scan_warning?.reason ? `Review: ${summary.scan_warning.reason}` : 'Review: no fallback warning flags.';
  const heading = `Final disposition from case ROA: ${finality}${date && !finality.includes(date) ? ` (${date})` : ''}.`;
  const summaryHtml = `<div class="cs-judgment-summary${view === 'table-summary' ? ' is-table' : ''}" data-judgment-state="case-status-fallback">`
    + `<div class="cs-judgment-nature">${escapeJudgmentHtml(heading)}</div>`
    + `<div class="cs-judgment-amount">${escapeJudgmentHtml(fallbackAmountDescription(summary))}</div>`
    + `<div class="cs-judgment-satisfaction">Satisfaction: ${escapeJudgmentHtml(summary.satisfaction_date ? `satisfaction signal on ${summary.satisfaction_date}` : (summary.satisfied ? 'satisfaction signal recorded' : 'no current full satisfaction recorded/not applicable'))}.</div>`
    + (appeal ? `<div class="cs-judgment-source">${escapeJudgmentHtml(appeal)}</div>` : '')
    + `<div class="cs-judgment-source">Judgment shard has no case record; this panel is using the loaded full-case status signal.</div>`
    + `</div>`;
  if (view === 'table-summary') return summaryHtml;
  const detailBody = `<div class="cs-kv-grid">`
    + kv('status', escapeJudgmentHtml(status || finality))
    + kv('finality', escapeJudgmentHtml(finality))
    + kv('date', `<span class="mono">${escapeJudgmentHtml(date || 'not recorded')}</span>`)
    + kv('judgment shard', escapeJudgmentHtml('no matching case record'))
    + kv('review', escapeJudgmentHtml(review))
    + `</div>`
    + `<div><b>Exact source text</b><pre class="mono" data-judgment-source-text>${escapeJudgmentHtml(source || 'No exact source snippet was available in the case-status fallback.')}</pre></div>`;
  const detailHtml = `<details class="cs-judgment-inline-details">`
    + `<summary>Judgment details</summary>`
    + detailBody
    + `</details>`;
  if (view === 'posture') {
    const satisfaction = fallbackSatisfactionText(summary);
    const satisfactionHtml = satisfaction
      ? `<div class="cs-judgment-satisfaction">${escapeJudgmentHtml(satisfaction)}</div>`
      : '';
    return `<div class="cs-judgment-posture" data-judgment-state="case-status-fallback">${satisfactionHtml}${detailHtml}</div>`;
  }
  return `<div data-judgment-state="case-status-fallback">${summaryHtml}${detailHtml}</div>`;
}

function renderJudgmentForView(record, view = 'panel', fallbackSummary = null) {
  if (view === 'table-summary') return renderJudgmentSummary(record, 'table');
  if (view === 'posture') return renderJudgmentPosture(record, fallbackSummary);
  return renderJudgmentRecord(record);
}

async function fetchJson(url, fetchImpl) {
  const response = await fetchImpl(url, { credentials: 'same-origin' });
  if (!response || !response.ok) {
    const error = new Error(`judgment fetch failed: ${response?.status ?? 'no response'}`);
    error.status = response?.status;
    throw error;
  }
  return response.json();
}

function validateManifest(manifest) {
  return manifest
    && EXPECTED_MANIFEST.schema_versions.includes(manifest.schema_version)
    && manifest.shard_pattern === EXPECTED_MANIFEST.shard_pattern
    && manifest.hash_algorithm === EXPECTED_MANIFEST.hash_algorithm;
}

function shardUrl(manifestUrl, pattern, shard) {
  const base = String(manifestUrl).replace(/[^/]*$/, '');
  return `${base}${pattern.replace('{shard}', shard)}`;
}

function rawShardUrl(rawBaseUrl, pattern, shard) {
  const base = String(rawBaseUrl || '').replace(/\/?$/, '/');
  return base ? `${base}${pattern.replace('{shard}', shard)}` : '';
}

async function fetchFirstJson(urls, fetchImpl) {
  let lastError = null;
  for (const url of urls) {
    if (!url) continue;
    try {
      return await fetchJson(url, fetchImpl);
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError || new Error('judgment fetch failed: no urls');
}

export async function loadJudgmentCase(caseNumber, options = {}) {
  const normalizedCaseNumber = normalizeJudgmentCaseNumber(caseNumber);
  if (!normalizedCaseNumber) return { status: 'missing_case_number', normalizedCaseNumber };
  const fetchImpl = options.fetchImpl || globalThis.fetch;
  const manifestUrl = options.manifestUrl || JUDGMENT_MANIFEST_URL;
  const rawBaseUrl = options.rawBaseUrl === undefined ? JUDGMENT_RAW_BASE_URL : options.rawBaseUrl;
  if (typeof fetchImpl !== 'function') return { status: 'unavailable', normalizedCaseNumber };

  const useCache = !options.fetchImpl && !options.manifestUrl;
  try {
    const loadManifest = () => fetchJson(manifestUrl, fetchImpl);
    const manifest = useCache
      ? await (manifestPromise || (manifestPromise = loadManifest().catch((error) => {
        manifestPromise = null;
        throw error;
      })))
      : await loadManifest();
    if (!validateManifest(manifest)) return { status: 'unsupported_schema', normalizedCaseNumber };

    const shard = await judgmentShardForCase(normalizedCaseNumber);
    const urls = [
      shardUrl(manifestUrl, manifest.shard_pattern, shard),
      rawShardUrl(rawBaseUrl, manifest.shard_pattern, shard),
    ].filter((url, index, arr) => url && arr.indexOf(url) === index);
    const cacheKey = `${manifestUrl}|${rawBaseUrl || ''}|${shard}`;
    const loadShard = () => fetchFirstJson(urls, fetchImpl);
    let payload;
    try {
      payload = useCache
        ? await (shardPromises.get(cacheKey) || (() => {
          const promise = loadShard().catch((error) => {
            shardPromises.delete(cacheKey);
            throw error;
          });
          shardPromises.set(cacheKey, promise);
          return promise;
        })())
        : await loadShard();
    } catch (error) {
      if (error?.status === 404) return { status: 'no_case', normalizedCaseNumber, shard };
      throw error;
    }
    if (!payload || payload.schema_version !== EXPECTED_SHARD_SCHEMA_VERSION || !payload.cases || typeof payload.cases !== 'object') {
      return { status: 'unsupported_schema', normalizedCaseNumber, shard };
    }
    const record = payload.cases[normalizedCaseNumber];
    return record
      ? { status: 'ready', normalizedCaseNumber, shard, record }
      : { status: 'no_case', normalizedCaseNumber, shard };
  } catch {
    return { status: 'unavailable', normalizedCaseNumber };
  }
}

export async function hydrateJudgmentPanels(root, caseNumber, options = {}) {
  if (!root || typeof root.querySelectorAll !== 'function') return { status: 'no_root' };
  const targets = [...root.querySelectorAll('[data-judgment-view]')];
  if (!targets.length) return { status: 'no_targets' };
  const normalized = normalizeJudgmentCaseNumber(caseNumber);
  const fallbackStatus = options.fallbackStatus || options.fallbackSummary || null;
  const result = await loadJudgmentCase(normalized, options);
  const canonicalStatus = canonicalCaseStatusSummary(
    result.status === 'ready' ? result.record : null,
    fallbackStatus,
  );
  targets.forEach((target) => {
    if (target.isConnected === false || target.dataset?.judgmentCase !== normalized) return;
    const view = target.dataset?.judgmentView || 'panel';
    if (result.status === 'ready') target.innerHTML = renderJudgmentForView(result.record, view, fallbackStatus);
    else if (result.status === 'no_case' || result.status === 'missing_case_number') {
      target.innerHTML = fallbackJudgmentHtml(fallbackStatus, view) || noCaseHtml(view);
    }
    else if (result.status === 'unsupported_schema') {
      target.innerHTML = unavailableHtml('Judgment data uses an unsupported schema and was not displayed.', view);
    } else {
      target.innerHTML = unavailableHtml('Judgment data is not available.', view);
    }
  });
  if (typeof options.onCanonicalStatus === 'function') {
    options.onCanonicalStatus(canonicalStatus, result);
  }
  return result;
}

export function resetJudgmentCacheForTests() {
  manifestPromise = null;
  shardPromises.clear();
}
