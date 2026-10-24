/**
 * Purchase Order Form Interactive Controls & Dynamic Approval Matrix Engine
 */

let approvalMatrixRules = [];
let deletedFileTracker = [];

// Fetch approval matrix rules on startup
function initApprovalMatrix() {
    fetch('/api/approval-matrix')
        .then(res => res.json())
        .then(data => {
            approvalMatrixRules = data;
            calculateSystemStatus(); // Recalculate once loaded
        })
        .catch(err => console.error("Error loading approval matrix:", err));
}

// Dynamic rule lookup helper based on database min/max and category
function getMatchingRule(expenseType, isBudgeted, estimatedCost) {
    if (!approvalMatrixRules.length) return null;

    let catKeyword = 'Operational';
    if (expenseType === 'Emergency') catKeyword = 'Emergency';
    else if (isBudgeted === 'No') catKeyword = 'Unbudgeted';
    else if (expenseType === 'Service Contract') catKeyword = 'Service Provider Contracts';
    else if (expenseType === 'Capital') catKeyword = 'Capital Expenditure';

    // 1. Match by category keyword and cost bounds
    const matching = approvalMatrixRules.filter(rule => {
        const matchesCategory = rule.matrix_category.toLowerCase().includes(catKeyword.toLowerCase());
        const matchesMin = estimatedCost >= parseFloat(rule.min_value);
        const matchesMax = rule.max_value === null || estimatedCost <= parseFloat(rule.max_value);
        return matchesCategory && matchesMin && matchesMax;
    });

    if (matching.length > 0) return matching[0];

    // 2. Fallback match by cost bounds alone
    return approvalMatrixRules.find(rule => {
        const matchesMin = estimatedCost >= parseFloat(rule.min_value);
        const matchesMax = rule.max_value === null || estimatedCost <= parseFloat(rule.max_value);
        return matchesMin && matchesMax;
    }) || null;
}

// Live filter JS for GL code dropdown search input
document.addEventListener('DOMContentLoaded', function() {
    initApprovalMatrix();

    const searchInput = document.getElementById('gl-search-input');
    const selectElement = document.getElementById('gl-code-select');

    if (searchInput && selectElement) {
        searchInput.addEventListener('input', function() {
            const filter = searchInput.value.toLowerCase();
            const options = selectElement.options;

            for (let i = 0; i < options.length; i++) {
                const text = options[i].text.toLowerCase();
                if (i === 0) {
                    options[i].style.display = "";
                    continue;
                }
                options[i].style.display = text.includes(filter) ? "" : "none";
            }
        });
    }

    // Auto-fetch financial metrics when GL Code selection changes
    document.getElementById('gl-code-select')?.addEventListener('change', function() {
        const glCode = this.value;
        if (!glCode) return;

        fetch(`/api/budget-details?gl_code=${encodeURIComponent(glCode)}`)
            .then(response => response.json())
            .then(data => {
                if (data) {
                    document.getElementById('fin-ytd-actual').value = data.ytd_actual ?? '';
                    document.getElementById('fin-total-budget').value = data.total_annual_budget ?? '';
                    document.getElementById('fin-budget-ytd').value = data.budget_ytd ?? '';
                    document.getElementById('fin-buffer-pool').value = data.buffer_pool ?? '';
                    document.getElementById('fin-variance').value = data.variance ?? '';
                    calculateSystemStatus();
                }
            })
            .catch(err => console.error('Error fetching budget details:', err));
    });

    const statusInputs = [
        'submission_status', 
        'is_budgeted', 
        'gl_code', 
        'expense_type', 
        'estimated_cost'
    ];

    statusInputs.forEach(fieldName => {
        const el = document.querySelector(`[name="${fieldName}"]`);
        if (el) {
            el.addEventListener('change', calculateSystemStatus);
            el.addEventListener('input', calculateSystemStatus);
        }
    });

    const clearBtn = document.getElementById('btn-clear-ai');
    if (clearBtn) {
        clearBtn.addEventListener('click', clearAiAnalysis);
    }

    // Form Submit Unbudgeted Guard
    const formEl = document.getElementById('po-form');
    if (formEl) {
        formEl.addEventListener('submit', function(e) {
            const submissionStatus = document.querySelector('[name="submission_status"]')?.value;
            const isBudgeted = document.querySelector('[name="is_budgeted"]')?.value;
            const totalBudget = parseFloat(document.getElementById('fin-total-budget')?.value) || 0;
            const ytdActual = parseFloat(document.getElementById('fin-ytd-actual')?.value) || 0;
            const estimatedCost = parseFloat(document.querySelector('[name="estimated_cost"]')?.value) || 0;

            if ((totalBudget > 0 && estimatedCost > (totalBudget - ytdActual)) || isBudgeted === 'No') {
                if (submissionStatus === 'Submit for Approval' || submissionStatus === 'Sent') {
                    e.preventDefault();
                    alert("❌ CANNOT SUBMIT FOR APPROVAL: Unbudgeted transactions must be routed through 'Submit for Finance Review' first.");
                }
            }
        });
    }

    // Render AI recommendation summary if available globally
    const aiDisplay = document.getElementById('ai-analysis-display');
    if (aiDisplay && window.initialAiSummary && window.initialAiSummary.trim() !== '') {
        const cleanHtml = window.initialAiSummary.replace(/AI_RECOMMENDATION_LINE:[\s\S]*/gi, '').trim();
        aiDisplay.innerHTML = cleanHtml;
    }

    calculateSystemStatus();
});

// Single consolidated function for handling file uploads & quote count
function updateQuotesCount(input) {
    const display = document.getElementById('quotes_count_display');
    const quotesField = document.getElementById('quotes_provided');
    const attachmentsList = document.getElementById('attachments_list');
    const statusText = document.getElementById('file_chosen_status');
    const files = input.files ? Array.from(input.files) : [];
    const count = files.length;

    if (statusText) {
        if (count === 1) {
            statusText.textContent = files[0].name;
            statusText.style.fontStyle = 'normal';
        } else if (count > 1) {
            statusText.textContent = `${count} files selected`;
            statusText.style.fontStyle = 'normal';
        } else {
            statusText.textContent = 'No file chosen';
            statusText.style.fontStyle = 'italic';
        }
    }

    if (quotesField && count > 0) {
        quotesField.value = count;
    }

    if (display) {
        display.style.display = count > 0 ? 'block' : 'none';
        display.textContent = count > 0 ? `${count} file(s) selected for upload.` : '';
    }

    if (attachmentsList && count > 0) {
        attachmentsList.innerHTML = '';
        files.forEach((file) => {
            const objectUrl = URL.createObjectURL(file);
            const ext = file.name.split('.').pop().toLowerCase();
            const icon = ['jpg', 'jpeg', 'png', 'webp'].includes(ext) ? '🖼️' : '📄';

            const badge = document.createElement('a');
            badge.href = objectUrl;
            badge.target = '_blank';
            badge.rel = 'noopener noreferrer';
            badge.className = 'doc-badge';
            badge.innerHTML = `<span class="doc-icon">${icon}</span> <span class="doc-name">${file.name}</span>`;

            attachmentsList.appendChild(badge);
        });
    }

    if (typeof calculateSystemStatus === 'function') {
        calculateSystemStatus();
    }
}

// Independent Governance Tier & Compliance Status Engine (Fully Dynamic from Approval Matrix)
function calculateSystemStatus() {
    const submissionStatusSelect = document.querySelector('[name="submission_status"]');
    const submissionStatus = submissionStatusSelect?.value || '';
    const isBudgetedSelect = document.querySelector('[name="is_budgeted"]');
    const glCode = document.querySelector('[name="gl_code"]')?.value || '';
    const expenseType = document.querySelector('[name="expense_type"]')?.value || '';
    const estimatedCost = parseFloat(document.querySelector('[name="estimated_cost"]')?.value) || 0;
    const quotesProvided = parseInt(document.getElementById('quotes_provided')?.value) || 0;
    
    const totalBudget = parseFloat(document.getElementById('fin-total-budget')?.value) || 0;
    const ytdActual = parseFloat(document.getElementById('fin-ytd-actual')?.value) || 0;
    
    const remainingAnnualBudget = totalBudget - ytdActual;
    const isOverAnnualBudget = totalBudget > 0 && estimatedCost > remainingAnnualBudget;

    // 1. HARD COMPLIANCE LOCK: Over Annual Budget forces Unbudgeted Workflow
    if (isOverAnnualBudget && isBudgetedSelect) {
        if (isBudgetedSelect.value !== 'No') {
            isBudgetedSelect.value = 'No';
            alert("⚠️ BUDGET EXCEEDED: The requested amount exceeds the remaining Total Annual Budget. This transaction has been automatically reclassified as 'Unbudgeted' and requires Extraordinary Board/HOA approval.");
        }
    }

    // 2. DISABLE/ENABLE SUBMISSION OPTIONS BASED ON BUDGET OVERRUN
    if (submissionStatusSelect) {
        Array.from(submissionStatusSelect.options).forEach(option => {
            if (option.value === 'Submit for Approval' || option.value === 'Sent') {
                if (isOverAnnualBudget || isBudgetedSelect?.value === 'No') {
                    option.disabled = true;
                    if (option.selected) {
                        submissionStatusSelect.value = 'Submit for Finance Review';
                    }
                } else {
                    option.disabled = false;
                }
            }
        });
    }

    const currentIsBudgeted = isBudgetedSelect?.value || '';

    // --- DYNAMIC RULE LOOKUP FROM APPROVAL MATRIX ---
    const rule = getMatchingRule(expenseType, currentIsBudgeted, estimatedCost);

    const requiredQuotes = rule ? rule.quotes_required : 0;
    const responseTimeLimit = rule ? rule.response_time_limit_days : null;
    const minResponses = rule ? rule.min_responses_required : null;
    const approverRoles = rule ? rule.approver_roles : 'Standard Sign-off';
    const categoryName = rule ? rule.matrix_category : 'Pending Context Classification';

    // --- PART A: CALCULATE APPROVAL REQUIREMENT (Governance & Delegation) ---
    let approvalRequirement = '';
    if (rule) {
        const timeText = responseTimeLimit ? `${responseTimeLimit}-day limit` : 'No time limit';
        const quorumText = minResponses ? `Min Quorum: ${minResponses}` : 'Single Approver';
        approvalRequirement = `${categoryName} (${requiredQuotes} Quotes Required | Approvers: ${approverRoles} | ${timeText}, ${quorumText})`;
    } else {
        approvalRequirement = 'Pending Context Classification';
    }

    // --- PART B: CALCULATE SYSTEM STATUS (Lifecycle & Compliance Blocks) ---
    let systemStatus = '';
    if (submissionStatus === 'Approved') {
        systemStatus = '✅ APPROVED: Compliance Process Complete';
    } else if (submissionStatus === 'Run AI Analysis') {
        systemStatus = '🤖 AI ANALYSIS: Reviewing Compliance & Budget Impact';
    } else if (submissionStatus === 'Submit for Finance Review') {
        systemStatus = '🔍 FINANCE REVIEW: Awaiting Finance Committee Sign-off';    
    } else if (submissionStatus === 'Sent') {
        systemStatus = '📬 SENT: Approval Request Dispatched via Email'; 
    } else if (submissionStatus === 'Rejected') {
        systemStatus = '🛑 REJECTED: Final Authorisation Declined';
    } else if ((isOverAnnualBudget || currentIsBudgeted === 'No') && quotesProvided < 3) {
        systemStatus = `❌ UNBUDGETED BLOCKED: Insufficient Quotes (${quotesProvided}/3 Required)`;
    } else if (currentIsBudgeted === 'Yes' && !glCode) {
        systemStatus = '⚠️ ACTION REQUIRED: Select a valid GL description';
    } else if (!expenseType) {
        systemStatus = '⚠️ ACTION REQUIRED: Select Expense Type';
    } else if (quotesProvided < requiredQuotes) {
        systemStatus = `❌ BLOCKED: Insufficient Quotes Provided (${quotesProvided}/${requiredQuotes} Required)`;
    } else {
        systemStatus = '🟢 READY: Compliance Met, Ready for Routing';
    }

    // Update Form Inputs / Outputs
    const systemStatusInput = document.getElementById('system_status') || document.querySelector('[name="system_status"]');
    if (systemStatusInput) {
        systemStatusInput.value = systemStatus;
    }

    // Update both top and bottom Governance Sign-off Tier fields simultaneously
    const reqInputTop = document.getElementById('approval_requirement');
    const reqInputBottom = document.getElementById('approval_requirement_copy');
    
    if (reqInputTop) reqInputTop.value = approvalRequirement;
    if (reqInputBottom) reqInputBottom.value = approvalRequirement;
    }
}

function removeAttachment(filename, elementId) {
    if (confirm(`Are you sure you want to remove "${filename}"?`)) {
        deletedFileTracker.push(filename);
        const deletedInput = document.getElementById('deleted_files');
        if (deletedInput) {
            deletedInput.value = deletedFileTracker.join(',');
        }

        const row = document.getElementById(elementId);
        if (row) row.remove();

        if (typeof clearAiAnalysis === 'function') clearAiAnalysis();
        if (typeof calculateSystemStatus === 'function') calculateSystemStatus();
    }
}

function clearAiAnalysis() {
    const aiTextarea = document.getElementById('ai_recommendation_summary');
    const aiDisplay = document.getElementById('ai-analysis-display');
    const clearFlag = document.getElementById('clear_ai_flag');
    const clearBtn = document.getElementById('btn-clear-ai');
    
    if (aiTextarea) aiTextarea.value = '';
    if (aiDisplay) aiDisplay.innerHTML = '<div class="ai-empty-state">No AI analysis generated for this purchase order yet.</div>';
    if (clearFlag) clearFlag.value = '1';
    if (clearBtn) clearBtn.style.display = 'none';
}

function checkApprovals() {
    const btn = document.getElementById('btn-check-approvals');
    const spinner = document.getElementById('check-spinner');
    const currentPo = window.initialPoNumber || '';

    if (btn) btn.disabled = true;
    if (spinner) spinner.style.display = 'inline';

    showToast("🔄 Approval check triggered. Waiting for GitHub Action...", "info");

    fetch('/api/check-approvals', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ po_number: currentPo })
    })
    .then(response => response.json())
    .then(data => {
        if (data.status === 'success') {
            pollWorkflowStatus();
        } else {
            showToast("⚠️ " + data.message, "error");
            resetButtonState(btn, spinner);
        }
    })
    .catch(err => {
        console.error('Error triggering approvals:', err);
        showToast("❌ Failed to trigger approval check.", "error");
        resetButtonState(btn, spinner);
    });
}

function pollWorkflowStatus() {
    const maxAttempts = 15;
    let attempts = 0;

    const interval = setInterval(() => {
        attempts++;
        fetch('/api/workflow-status')
            .then(res => res.json())
            .then(data => {
                if (data.workflow_status === 'completed') {
                    clearInterval(interval);
                    showToast("✅ GitHub Action complete! Refreshing page...", "success");
                    setTimeout(() => { window.location.reload(); }, 1500);
                } else if (attempts >= maxAttempts) {
                    clearInterval(interval);
                    showToast("⚠️ Workflow is taking longer than usual. Refreshing anyway...", "info");
                    setTimeout(() => { window.location.reload(); }, 2000);
                } else {
                    showToast(`⏳ Processing emails via Gemini... (Attempt ${attempts}/${maxAttempts})`, "info");
                }
            })
            .catch(err => { console.error('Polling error:', err); });
    }, 10000);
}

function resetButtonState(btn, spinner) {
    if (btn) btn.disabled = false;
    if (spinner) spinner.style.display = 'none';
}

function showToast(message, type = 'success') {
    let existingToast = document.getElementById('app-toast-notification');
    if (existingToast) existingToast.remove();

    const toast = document.createElement('div');
    toast.id = 'app-toast-notification';
    toast.textContent = message;
    
    let bgColor = '#212529';
    if (type === 'success') bgColor = '#198754';
    if (type === 'error') bgColor = '#dc3545';
    if (type === 'info') bgColor = '#0d6efd';

    toast.style.cssText = `
        position: fixed;
        top: 20px;
        right: 20px;
        background-color: ${bgColor};
        color: white;
        padding: 12px 20px;
        border-radius: 6px;
        box-shadow: 0 4px 12px rgba(0,0,0,0.15);
        font-size: 14px;
        font-weight: 500;
        z-index: 9999;
        transition: opacity 0.3s ease;
    `;

    document.body.appendChild(toast);
    
    if (type !== 'success') {
        setTimeout(() => { toast.remove(); }, 4000);
    }
}
