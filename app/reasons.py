REASONS = {
    'COLUMN_COUNT':'列数错误', 'MISSING_ID':'编号缺失', 'DUPLICATE_CONFLICT':'重复编号冲突',
    'MISSING_VALUE':'数值缺失', 'BAD_NUMBER':'数值格式错误', 'BAD_TIME':'时间格式错误',
    'BAD_UNIT':'单位不受支持', 'UNKNOWN_REFERENCE':'引用对象不存在', 'INVALID_RANGE':'范围错误',
    'VERSION_CONFLICT':'版本冲突', 'EMPTY_FILE':'空文件', 'BLANK_FILE':'全空行',
    'HEADER_ONLY':'仅有表头', 'STALE_VERSION':'复核版本已更新', 'NOT_FOUND':'批次不存在',
    'BAD_MERGE_INPUT':'合并入参错误', 'UNKNOWN_ACCOUNT':'账户无历史依据',
    'MERGE_ID_CONFLICT':'同编号原值冲突', 'HISTORY_UNSTABLE':'历史归属会改写',
    'RECONCILE_FAILED':'合并对账未通过', 'MERGE_OVERLAP':'账户已在待对账合并中',
    'MERGE_ALREADY_SETTLED':'账户已合并生效', 'MERGE_NOT_FOUND':'合并记录不存在',
    'MERGE_NOT_PENDING':'合并状态不允许该操作', 'MERGE_STALE':'合并依据已变化',
    'MERGE_UNBALANCED':'合并积分不守恒', 'BAD_REVISION':'版本号无效',
}
def reject(state, record, code, detail):
    state['quarantine'].append({'line':record.get('_line',0),'id':record.get('id',''),
       'code':code,'detail':detail,'raw':{k:v for k,v in record.items() if not k.startswith('_')}})
