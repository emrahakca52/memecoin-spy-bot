#property strict
#property version   "1.00"
#property description "XAUUSD EMA/ATR demo-only expert advisor. Refuses non-demo accounts."

#include <Trade/Trade.mqh>
CTrade trade;

input string          InpTradeSymbol       = "XAUUSD";
input ENUM_TIMEFRAMES InpTimeframe         = PERIOD_M5;
input int             InpFastEMA           = 9;
input int             InpSlowEMA           = 21;
input int             InpATRPeriod         = 14;
input double          InpATRStopMultiplier= 1.8;
input double          InpRewardRisk        = 1.5;
input double          InpRiskPercent       = 0.25;
input int             InpMaxSpreadPoints   = 80;
input ulong           InpMagic             = 28102026;
input int             InpDeviationPoints   = 20;

int fastHandle = INVALID_HANDLE;
int slowHandle = INVALID_HANDLE;
int atrHandle  = INVALID_HANDLE;
datetime lastBarTime = 0;
string tradeSymbol = "";

bool IsDemoAccount()
{
   return ((ENUM_ACCOUNT_TRADE_MODE)AccountInfoInteger(ACCOUNT_TRADE_MODE) == ACCOUNT_TRADE_MODE_DEMO);
}

bool IsGoldSymbol(const string symbol)
{
   // Allows common broker suffixes such as XAUUSD.a or XAUUSDm.
   return (StringFind(symbol, InpTradeSymbol) == 0);
}

bool IsNewBar()
{
   datetime t[1];
   if(CopyTime(tradeSymbol, InpTimeframe, 0, 1, t) != 1)
      return false;
   if(t[0] == lastBarTime)
      return false;
   lastBarTime = t[0];
   return true;
}

bool HasOurPosition()
{
   for(int i = PositionsTotal() - 1; i >= 0; --i)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) == tradeSymbol &&
         (ulong)PositionGetInteger(POSITION_MAGIC) == InpMagic)
         return true;
   }
   return false;
}

double NormalizeVolume(double volume)
{
   double vmin = SymbolInfoDouble(tradeSymbol, SYMBOL_VOLUME_MIN);
   double vmax = SymbolInfoDouble(tradeSymbol, SYMBOL_VOLUME_MAX);
   double step = SymbolInfoDouble(tradeSymbol, SYMBOL_VOLUME_STEP);
   if(step <= 0.0)
      return 0.0;

   volume = MathMin(vmax, MathMax(vmin, MathFloor(volume / step) * step));
   int digits = 0;
   double s = step;
   while(digits < 8 && MathAbs(s - MathRound(s)) > 1e-8)
   {
      s *= 10.0;
      digits++;
   }
   return NormalizeDouble(volume, digits);
}

double CalculateVolume(const double entry, const double stop)
{
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double riskMoney = equity * InpRiskPercent / 100.0;
   double tickSize = SymbolInfoDouble(tradeSymbol, SYMBOL_TRADE_TICK_SIZE);
   double tickValue = SymbolInfoDouble(tradeSymbol, SYMBOL_TRADE_TICK_VALUE);
   double distance = MathAbs(entry - stop);

   if(riskMoney <= 0.0 || tickSize <= 0.0 || tickValue <= 0.0 || distance <= 0.0)
      return 0.0;

   double lossPerLot = (distance / tickSize) * tickValue;
   if(lossPerLot <= 0.0)
      return 0.0;

   double rawVolume = riskMoney / lossPerLot;
   double minVolume = SymbolInfoDouble(tradeSymbol, SYMBOL_VOLUME_MIN);
   // Do not force minimum lot if it would exceed the configured risk budget.
   if(rawVolume < minVolume)
      return 0.0;

   return NormalizeVolume(rawVolume);
}

void OnTick()
{
   if(!IsDemoAccount())
      return;

   if(!IsNewBar())
      return;

   if(HasOurPosition())
      return;

   MqlTick tick;
   if(!SymbolInfoTick(tradeSymbol, tick) || tick.ask <= 0.0 || tick.bid <= 0.0)
      return;

   double point = SymbolInfoDouble(tradeSymbol, SYMBOL_POINT);
   if(point <= 0.0)
      return;

   double spreadPoints = (tick.ask - tick.bid) / point;
   if(spreadPoints > InpMaxSpreadPoints)
      return;

   double fast[3], slow[3], atr[2];
   ArraySetAsSeries(fast, true);
   ArraySetAsSeries(slow, true);
   ArraySetAsSeries(atr, true);

   if(CopyBuffer(fastHandle, 0, 0, 3, fast) != 3 ||
      CopyBuffer(slowHandle, 0, 0, 3, slow) != 3 ||
      CopyBuffer(atrHandle, 0, 0, 2, atr) != 2)
      return;

   if(atr[1] <= 0.0)
      return;

   // Use closed candles (index 1 and 2) to avoid acting on an unfinished bar.
   bool buySignal  = (fast[2] <= slow[2] && fast[1] > slow[1]);
   bool sellSignal = (fast[2] >= slow[2] && fast[1] < slow[1]);
   if(!buySignal && !sellSignal)
      return;

   double stopDistance = atr[1] * InpATRStopMultiplier;
   if(stopDistance <= 0.0)
      return;

   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints(InpDeviationPoints);
   trade.SetTypeFillingBySymbol(tradeSymbol);

   if(buySignal)
   {
      double entry = tick.ask;
      double sl = NormalizeDouble(entry - stopDistance, (int)SymbolInfoInteger(tradeSymbol, SYMBOL_DIGITS));
      double tp = NormalizeDouble(entry + stopDistance * InpRewardRisk, (int)SymbolInfoInteger(tradeSymbol, SYMBOL_DIGITS));
      double volume = CalculateVolume(entry, sl);
      if(volume > 0.0)
         trade.Buy(volume, tradeSymbol, 0.0, sl, tp, "XAUUSD demo EMA cross");
   }
   else if(sellSignal)
   {
      double entry = tick.bid;
      double sl = NormalizeDouble(entry + stopDistance, (int)SymbolInfoInteger(tradeSymbol, SYMBOL_DIGITS));
      double tp = NormalizeDouble(entry - stopDistance * InpRewardRisk, (int)SymbolInfoInteger(tradeSymbol, SYMBOL_DIGITS));
      double volume = CalculateVolume(entry, sl);
      if(volume > 0.0)
         trade.Sell(volume, tradeSymbol, 0.0, sl, tp, "XAUUSD demo EMA cross");
   }
}

int OnInit()
{
   if(!IsDemoAccount())
   {
      Print("SAFETY STOP: This EA is demo-account only. Initialization refused.");
      return INIT_FAILED;
   }

   tradeSymbol = (InpTradeSymbol == "" ? _Symbol : InpTradeSymbol);
   if(!IsGoldSymbol(tradeSymbol))
   {
      Print("SAFETY STOP: This EA is configured for XAUUSD symbols only.");
      return INIT_FAILED;
   }

   if(InpFastEMA < 2 || InpSlowEMA <= InpFastEMA || InpATRPeriod < 2 ||
      InpATRStopMultiplier <= 0.0 || InpRewardRisk <= 0.0 ||
      InpRiskPercent <= 0.0 || InpRiskPercent > 1.0)
   {
      Print("Invalid inputs. RiskPercent must be > 0 and <= 1.0.");
      return INIT_PARAMETERS_INCORRECT;
   }

   if(!SymbolSelect(tradeSymbol, true))
   {
      Print("Could not select symbol: ", tradeSymbol);
      return INIT_FAILED;
   }

   fastHandle = iMA(tradeSymbol, InpTimeframe, InpFastEMA, 0, MODE_EMA, PRICE_CLOSE);
   slowHandle = iMA(tradeSymbol, InpTimeframe, InpSlowEMA, 0, MODE_EMA, PRICE_CLOSE);
   atrHandle = iATR(tradeSymbol, InpTimeframe, InpATRPeriod);

   if(fastHandle == INVALID_HANDLE || slowHandle == INVALID_HANDLE || atrHandle == INVALID_HANDLE)
   {
      Print("Could not create indicator handles.");
      return INIT_FAILED;
   }

   Print("XAUUSD demo EA initialized. No real-account trading is permitted.");
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   if(fastHandle != INVALID_HANDLE) IndicatorRelease(fastHandle);
   if(slowHandle != INVALID_HANDLE) IndicatorRelease(slowHandle);
   if(atrHandle != INVALID_HANDLE) IndicatorRelease(atrHandle);
}
