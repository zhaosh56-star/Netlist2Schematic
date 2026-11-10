"""Teaching examples for the existing structure recognizers, not layout rules.

Each entry includes the context needed for a real recognizer match. Coordinates
only control this explanatory gallery; production placement never imports it.
"""
from __future__ import annotations


def example(slug, title, kind, netlist, positions, focus, roles, rule, note='',
            *, engine='structured'):
    return dict(slug=slug, title=title, kind=kind, netlist=netlist.strip() + '\n.end\n',
                positions=positions, focus=focus, roles=roles, rule=rule,
                note=note, engine=engine)


FOLDED = '''
MIP1 dl inp tail tail PMOS
MIP2 dr inn tail tail PMOS
MF1 ol vc dl 0 NMOS
MF2 or vc dr 0 NMOS
MB1 dl vb 0 0 NMOS
MB2 dr vb 0 0 NMOS
IT VDD tail 10u
'''
FOLDED_POSITIONS = {'IT': (0, 2, 0), 'MF1': (1, 0, 1), 'MIP1': (1, 1, 0),
                    'MIP2': (1, 3, 1), 'MF2': (1, 4, 0),
                    'MB1': (2, 1, 1), 'MB2': (2, 3, 0)}

CASES = [
    example('01-differential-pair', '差分对', 'differential_pair', '''
M1 dl inp tail 0 NMOS
M2 dr inn tail 0 NMOS
IT tail 0 10u
''', {'M1': (1, 1, 0), 'M2': (1, 3, 1), 'IT': (2, 2, 0)},
        ['M1', 'M2'], {'M1': '输入支路A', 'M2': '输入支路B', 'IT': '尾电流源（识别证据）'},
        '同型两管共源、栅和漏分别不同，并有尾部器件或电流源证据。',
        '蓝色成员是两只输入管；尾电流源是必须保留的识别上下文，不属于本条差分对成员。示例为NMOS，规则也支持PMOS。'),
    example('02-current-mirror', '电流镜', 'current_mirror', '''
MREF ref ref 0 0 NMOS
MOUT out ref 0 0 NMOS
''', {'MREF': (1, 1, 1), 'MOUT': (1, 3, 0)}, ['MREF', 'MOUT'],
        {'MREF': '参考管：D=G', 'MOUT': '输出管'},
        '同型管共源、共栅，至少有一只二极管接法参考管及一只输出管。',
        '默认识别器同样支持一只参考管带多个输出管；它们仍归入current_mirror。'),
    example('03-cross-coupled', '交叉耦合对', 'cross_coupled', '''
M1 q qb 0 0 NMOS
M2 qb q 0 0 NMOS
''', {'M1': (1, 1, 1), 'M2': (1, 3, 0)}, ['M1', 'M2'],
        {'M1': '支路A：D=q，G=qb', 'M2': '支路B：D=qb，G=q'},
        '同型管共源，两管的漏与对方栅交叉连接。',
        '异网十字交叉不画圆点；同网真实分支才画圆点。'),
    example('04-complementary-branch', '互补支路', 'complementary_branch', '''
MP out in VDD VDD PMOS
MN out in 0 0 NMOS
''', {'MP': (0, 2, 0), 'MN': (1, 2, 0)}, ['MP', 'MN'],
        {'MP': '上管PMOS', 'MN': '下管NMOS'},
        '一只PMOS与一只NMOS共漏、共栅，保留上下管角色。',
        '这是常见CMOS反相支路。识别只说明连接结构，不验证偏置和逻辑功能。'),
    example('05-output-pair', '互补输出级', 'output_pair', '''
MP out bias_p VDD VDD PMOS
MN out bias_n 0 0 NMOS
''', {'MP': (0, 2, 0), 'MN': (1, 2, 0)}, ['MP', 'MN'],
        {'MP': '输出上管：控制bias_p', 'MN': '输出下管：控制bias_n'},
        '一对PMOS/NMOS共漏、栅不同，源端接识别到的电源轨。',
        '与上一图的区别是两只管子使用不同的控制网；当前规则只对该输出节点唯一的P/N组合作这种识别。'),
    example('06-latch', '锁存核心', 'latch', '''
MPL q qb VDD VDD PMOS
MNL q qb 0 0 NMOS
MPR qb q VDD VDD PMOS
MNR qb q 0 0 NMOS
''', {'MPL': (0, 1, 1), 'MPR': (0, 3, 0),
      'MNL': (2, 1, 1), 'MNR': (2, 3, 0)}, ['MPL', 'MPR', 'MNL', 'MNR'],
        {'MPL': '左支路上管', 'MNL': '左支路下管',
         'MPR': '右支路上管', 'MNR': '右支路下管'},
        '交叉耦合关系与两条互补支路组合为一个父结构。',
        '可同时识别交叉耦合对和互补子支路；它们是父子关系，不是互相独占后只留一个。'),
    example('07-folded-core', '折叠共栅核心', 'folded_core', FOLDED,
        FOLDED_POSITIONS, ['MIP1', 'MIP2', 'MF1', 'MF2', 'MB1', 'MB2'],
        {'MIP1': '输入支路A', 'MIP2': '输入支路B', 'MF1': '折叠支路A',
         'MF2': '折叠支路B', 'MB1': '偏置支路A', 'MB2': '偏置支路B', 'IT': '尾部供给（上下文）'},
        '差分对、相反极性偏置支路和共栅折叠支路满足六管连接关系。',
        '六只管子是核心成员；IT是差分对尾部证据。下面的共栅关系示例保留相同上下文，只突出MF1/MF2。'),
    example('08-common-bias-pair', '共偏置关系', 'common_bias_pair', '''
M1 d1 vb 0 0 NMOS
M2 d2 vb 0 0 NMOS
''', {'M1': (1, 1, 1), 'M2': (1, 3, 0)}, ['M1', 'M2'],
        {'M1': '支路A', 'M2': '支路B'},
        '同型管共源、共栅，但缺少D=G的参考管，漏连接不同。',
        '这是relation辅助关系，不把它冒充电流镜，也不直接当成差分对。'),
    example('09-common-source-bank', '公共源支路', 'common_source_bank', '''
M1 d1 g1 shared 0 NMOS
M2 d2 g2 shared 0 NMOS
M3 d3 g3 shared 0 NMOS
IT shared 0 10u
''', {'M1': (1, 0, 0), 'M2': (1, 2, 0), 'M3': (1, 4, 1), 'IT': (2, 2, 0)},
        ['M1', 'M2', 'M3'], {'M1': '公共源成员1', 'M2': '公共源成员2',
                            'M3': '公共源成员3', 'IT': '公共节点引出（上下文）'},
        '至少三只MOS共享同一个源节点。',
        '这是relation关系。多管共源不能仅凭任意选两只就认成差分对。'),
    example('10-common-gate-pair', '共栅关系（折叠核心内）', 'common_gate_pair',
        FOLDED, FOLDED_POSITIONS, ['MF1', 'MF2'],
        {'MF1': '共栅折叠支路A', 'MF2': '共栅折叠支路B',
         'MIP1': '输入上下文A', 'MIP2': '输入上下文B',
         'MB1': '偏置上下文A', 'MB2': '偏置上下文B', 'IT': '尾部供给上下文'},
        '当前实现识别折叠核心时，为其中两只同栅折叠管派生该关系。',
        '不能删去周围器件后仍宣称本识别器会将任意同栅两管单独报告为common_gate_pair；本图保留完整识别上下文。'),
    example('11-series-branch', '有源串接支路', 'series_branch', '''
MUP out vb_up mid 0 NMOS
MLOW mid vb_low 0 0 NMOS
''', {'MUP': (0, 2, 0), 'MLOW': (1, 2, 0)}, ['MUP', 'MLOW'],
        {'MUP': '上管', 'MLOW': '下管'},
        '同型MOS的一只漏端连接另一只源端，保留上下串接关系。',
        '这是relation，帮助对齐支路，不等于旧代码中未启用的CAS模板被启用了。'),
    example('12-series-chain', '无源串联链', 'series_chain', '''
R1 n1 input 1k
L1 n2 n1 1m
C1 output n2 1p
''', {'R1': (1, 0, 1), 'L1': (1, 2, 1), 'C1': (1, 4, 1)}, ['R1', 'L1', 'C1'],
        {'R1': '串联成员1', 'L1': '串联成员2', 'C1': '串联成员3'},
        '内部连接点只有两条无源支路，且不被有源/源件端子或电源轨打断。',
        '保留连接次序；没有运放也可以识别串联链。'),
    example('13-parallel-passives', '并联无源支路', 'parallel_passives', '''
R1 node 0 10k
C1 node 0 1p
''', {'R1': (1, 1, 0), 'C1': (1, 3, 0)}, ['R1', 'C1'],
        {'R1': '并联电阻', 'C1': '并联电容'},
        '两个或多个无源件连接同一对网络，不要求器件类型相同。'),
    example('14-negative-feedback', '负反馈网络', 'feedback_network', '''
E1 out 0 vin fb 100k
RF out fb 10k
RG fb 0 1k
''', {'E1': (1, 2, 0), 'RF': (0, 2, 1), 'RG': (0, 0, 0)}, ['RF', 'RG'],
        {'E1': '运放代理（上下文）', 'RF': 'OUT→IN−反馈路径', 'RG': '输入节点接地支路'},
        '从E运放代理的OUT沿无源路径到IN−，并收集该输入节点的接地支路。',
        'E1提供反馈上下文，但不在feedback_network的devices里；加入E1的父块是opamp_core。'),
    example('15-positive-feedback', '正反馈网络', 'feedback_network', '''
E1 out 0 fb vin 100k
RF out fb 10k
RG fb 0 1k
''', {'E1': (1, 2, 0), 'RF': (2, 2, 1), 'RG': (2, 0, 0)}, ['RF', 'RG'],
        {'E1': '运放代理（上下文）', 'RF': 'OUT→IN+反馈路径', 'RG': '输入节点接地支路'},
        '同样是feedback_network，但反馈路径实际接到IN+，会标为正反馈。',
        '单独增加此图用于区分IN+与IN−，不是新增了一类识别算法。'),
    example('16-opamp-core', '运放反馈核心', 'opamp_core', '''
E1 out 0 fp fm 100k
RP1 out np 1k
RP2 np fp 1k
CP fp 0 1p
RF out fm 10k
RG fm 0 1k
''', {'E1': (1, 2, 0), 'RP1': (2, 3, 1), 'RP2': (2, 1, 1),
      'CP': (2, 0, 0), 'RF': (0, 2, 1), 'RG': (0, 0, 0)},
        ['E1', 'RP1', 'RP2', 'CP', 'RF', 'RG'],
        {'E1': '核心运放', 'RP1': '正反馈串联1', 'RP2': '正反馈串联2',
         'CP': '正反馈侧接地电容', 'RF': '负反馈电阻', 'RG': '负反馈侧接地电阻'},
        '一个运放代理及其识别到的正/负反馈网络组成父块。',
        '内部可同时有feedback_network、series_chain、parallel_passives等子结构；图为识别拓扑示例，不声称具有指定动态功能。'),
    example('17-legacy-transmission-gate', '传输门', '传输门', '''
MN out en input 0 NMOS
MP out en_b input VDD PMOS
''', {'MN': (0, 2, 1), 'MP': (1, 2, 1)}, ['MN', 'MP'],
        {'MN': 'NMOS开关支路', 'MP': 'PMOS开关支路'},
        '旧VF2模板：N/P管同源、同漏，栅网分别连接。',
        '属于旧参考识别器，不是默认新识别器新增的高置信类型；代码只核对拓扑，不验证en/en_b信号真的互补。', engine='legacy'),
    example('18-legacy-multiple-mirror', '多路电流镜', '多路电流镜', '''
MREF ref ref 0 0 NMOS
MO1 out1 ref 0 0 NMOS
MO2 out2 ref 0 0 NMOS
''', {'MREF': (1, 0, 1), 'MO1': (1, 2, 0), 'MO2': (1, 4, 0)}, ['MREF', 'MO1', 'MO2'],
        {'MREF': '二极管接法参考', 'MO1': '输出1', 'MO2': '输出2'},
        '旧VF2给一参考加多输出的3/4管镜单独的名称。',
        '默认新识别器也能报告这组连接，但类型统一为current_mirror，不单独叫“多路电流镜”。', engine='legacy'),
    example('19-legacy-parallel-mos', '同源同漏MOS组', '并联组', '''
M1 drain g1 0 0 NMOS
M2 drain g2 0 0 NMOS
''', {'M1': (1, 1, 0), 'M2': (1, 3, 1)}, ['M1', 'M2'],
        {'M1': '同源同漏成员1', 'M2': '同源同漏成员2'},
        '旧VF2的并联组：同型、同源、同漏，允许栅不同。',
        '与并联R/C的parallel_passives不同。栅不同的两管并不等于相同控制的电气并联器件。', engine='legacy'),
    example('20-legacy-diode-load', '二极管接法MOS负载', '二极管负载', '''
MREF bias bias 0 0 NMOS
''', {'MREF': (1, 2, 0)}, ['MREF'], {'MREF': 'D=G的MOS'},
        '旧VF2单管模板：MOS的D与G接同一网。',
        '这是MOS二极管接法，和网表前缀D的独立二极管器件不是一回事；单管不满足新电流镜的“参考+输出”条件。', engine='legacy'),
]
