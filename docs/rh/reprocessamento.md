# Reprocessamento de Horas no Ponto (RH)

O Sistur permite o apontamento de horas retroativamente e o reprocessamento em lote do saldo de horas de um funcionário num período. Esta página descreve as regras de negócio deste funcionamento.

## 1. O Problema da Retroatividade
Quando as regras de contrato de um funcionário mudam (ex: a carga horária passa de 8h para 6h na tela de cadastro), essa mudança afeta **daqui pra frente**.
No modelo do Sistur, toda vez que um funcionário bate o ponto em um determinado dia (criando o `TimeDay`), o sistema realiza um *Snapshot* do contrato daquele dia:
- `expected_minutes_snapshot` (Carga horária diária)
- `tolerance_snapshot` (Tolerância legal diária de minutos, geralmente 10)

Logo, as alterações no perfil do funcionário **NÃO** alteram os saldos de ontem ou do mês passado de forma automática.

## 2. A Ferramenta "Reprocessar Ponto"
No cadastro de qualquer funcionário na área de RH, existe um botão **"Reprocessar Ponto"**. Ele abre um modal permitindo recalcular em lote todos os dias de um período.

Isso é útil em dois cenários principais:

### Cenário A: Correção de Bug Sistêmico
Houve uma falha na lógica do cálculo e os saldos dos últimos meses ficaram levemente errados.
**Solução**: Acessar o modal, colocar as datas (Início e Fim) e **não marcar o checkbox**.
**Impacto**: O sistema vai em cada dia, pega as batidas originais e recalcula o saldo matemático, mas vai referênciar a jornada da época (`snapshot`) que foi congelada lá. O histórico contratual da pessoa será mantido intocável.

### Cenário B: Ajuste Contratual Retroativo
O RH percebeu que a pessoa foi contratada faz 2 meses com 8 horas diárias, mas o correto seria 6 horas. O cadastro físico foi consertado *hoje*, as batidas novas cairão como 6h, mas os 2 meses passados ficaram registrados como debedores porque a meta era 8h.
**Solução**: O RH deve ir no cadastro, consertar a jornada ali para 6h diárias, e então clicar em **Reprocessar Ponto** marcando o checkbox **"Sobrescrever carga horária?"**.
**Impacto**: O sistema vai em cada dia, e além de recalcular usando a matemática atualizada, vai ignorar o `snapshot` salvamento e forçá-lo com a jornada de 6h (jornada atual salva). 

## 3. Impacto na Auditoria (AuditLog)
Como a ferramenta atua sobre múltiplos registros (de dezenas a centenas de dias para um funcionário), processar ela gera um **log massivo** e comprimido sob o tipo `mass_reprocess_update` (e não polui o log normal dia a dia).

Estará visível os deltas entre as horas trabalhadas, o saldo em minutos e a meta de carga horária para cada um dos dias afetados do intervalo.

## 4. Segurança / Dias em Aberto
Dias com batidas **Ímpares** (exemplo, só bateu na Entrada e não bateu na Saída) continuarão sendo ignoradas pelo saldo final. O status `needs_review` será marcado. O reprocessamento não corrige falta de batida.
