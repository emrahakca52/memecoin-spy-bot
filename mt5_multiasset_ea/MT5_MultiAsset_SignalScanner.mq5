//+------------------------------------------------------------------+
//| MT5 Multi-Asset Signal Scanner - signal only, no order execution |
//| Uses only the broker's MT5 terminal feed.                        |
//+------------------------------------------------------------------+
#property strict
#property version   "1.00"
#property description "Scans broker symbols and sends manual-trading zones. Never places orders."

input int    ScanEverySeconds = 300;
input int    MaxSymbolsPerScan = 0; // 0 = scan all broker symbols
input int    FastEMA = 20;
input int    SlowEMA = 50;
input int    RSIPeriod = 14;
input int    ATRPeriod = 14;
input double MinATRPoints = 0;
input bool   EnablePush = true;
input bool   EnableTelegram = false;
input string TelegramBotToken = "";
input string TelegramChatId = "";
input int    MinimumSignalScore = 4;

datetime last_signal_time[];
string   last_signal_symbol[];
int      last_signal_direction[];

bool ReadBufferValue(const int handle,const int buffer,const int shift,double &value)
{
   if(handle==INVALID_HANDLE) return false;
   double data[];
   ArraySetAsSeries(data,true);
   if(CopyBuffer(handle,buffer,shift,1,data)!=1) return false;
   value=data[0];
   return true;
}

string PriceText(const string symbol,const double price)
{
   int digits=(int)SymbolInfoInteger(symbol,SYMBOL_DIGITS);
   return DoubleToString(price,digits);
}

string JsonEscape(string s)
{
   StringReplace(s,"\\","\\\\");
   StringReplace(s,"\"","\\\"");
   StringReplace(s,"\r","");
   StringReplace(s,"\n","\\n");
   return s;
}

bool SendTelegram(const string message)
{
   if(!EnableTelegram || TelegramBotToken=="" || TelegramChatId=="") return false;
   string url="https://api.telegram.org/bot"+TelegramBotToken+"/sendMessage";
   string headers="Content-Type: application/json\r\n";
   string body="{\"chat_id\":\""+JsonEscape(TelegramChatId)+"\",\"text\":\""+JsonEscape(message)+"\"}";
   char post[],result[];
   string result_headers;
   StringToCharArray(body,post,0,WHOLE_ARRAY,CP_UTF8);
   ArrayResize(post,ArraySize(post)-1);
   ResetLastError();
   int code=WebRequest("POST",url,headers,8000,post,result,result_headers);
   if(code==-1)
   {
      Print("Telegram WebRequest error: ",GetLastError(),
            ". Add https://api.telegram.org to MT5 Tools > Options > Expert Advisors > WebRequest allowlist.");
      return false;
   }
   return (code==200);
}

bool IsDuplicate(const string symbol,const int direction)
{
   datetime now=TimeCurrent();
   for(int i=0;i<ArraySize(last_signal_symbol);i++)
   {
      if(last_signal_symbol[i]==symbol && last_signal_direction[i]==direction &&
         now-last_signal_time[i]<1800) return true;
   }
   return false;
}

void RememberSignal(const string symbol,const int direction)
{
   int n=ArraySize(last_signal_symbol);
   ArrayResize(last_signal_symbol,n+1);
   ArrayResize(last_signal_direction,n+1);
   ArrayResize(last_signal_time,n+1);
   last_signal_symbol[n]=symbol;
   last_signal_direction[n]=direction;
   last_signal_time[n]=TimeCurrent();
}

void AnalyzeSymbol(const string symbol)
{
   if(!SymbolSelect(symbol,true)) return;
   MqlTick tick;
   if(!SymbolInfoTick(symbol,tick) || tick.bid<=0 || tick.ask<=0) return;

   int hFast=iMA(symbol,PERIOD_M5,FastEMA,0,MODE_EMA,PRICE_CLOSE);
   int hSlow=iMA(symbol,PERIOD_M5,SlowEMA,0,MODE_EMA,PRICE_CLOSE);
   int hFastH1=iMA(symbol,PERIOD_H1,FastEMA,0,MODE_EMA,PRICE_CLOSE);
   int hSlowH1=iMA(symbol,PERIOD_H1,SlowEMA,0,MODE_EMA,PRICE_CLOSE);
   int hRsi=iRSI(symbol,PERIOD_M5,RSIPeriod,PRICE_CLOSE);
   int hAtr=iATR(symbol,PERIOD_M5,ATRPeriod);
   if(hFast==INVALID_HANDLE || hSlow==INVALID_HANDLE || hFastH1==INVALID_HANDLE ||
      hSlowH1==INVALID_HANDLE || hRsi==INVALID_HANDLE || hAtr==INVALID_HANDLE)
   {
      if(hFast!=INVALID_HANDLE) IndicatorRelease(hFast);
      if(hSlow!=INVALID_HANDLE) IndicatorRelease(hSlow);
      if(hFastH1!=INVALID_HANDLE) IndicatorRelease(hFastH1);
      if(hSlowH1!=INVALID_HANDLE) IndicatorRelease(hSlowH1);
      if(hRsi!=INVALID_HANDLE) IndicatorRelease(hRsi);
      if(hAtr!=INVALID_HANDLE) IndicatorRelease(hAtr);
      return;
   }

   double f,s,fh,sh,rsi,atr;
   bool ok=ReadBufferValue(hFast,0,1,f) && ReadBufferValue(hSlow,0,1,s) &&
           ReadBufferValue(hFastH1,0,1,fh) && ReadBufferValue(hSlowH1,0,1,sh) &&
           ReadBufferValue(hRsi,0,1,rsi) && ReadBufferValue(hAtr,0,1,atr);
   IndicatorRelease(hFast); IndicatorRelease(hSlow); IndicatorRelease(hFastH1);
   IndicatorRelease(hSlowH1); IndicatorRelease(hRsi); IndicatorRelease(hAtr);
   if(!ok || atr<=0) return;

   double point=SymbolInfoDouble(symbol,SYMBOL_POINT);
   if(point<=0 || (MinATRPoints>0 && atr/point<MinATRPoints)) return;

   int buyScore=0,sellScore=0;
   if(f>s) buyScore++; else if(f<s) sellScore++;
   if(fh>sh) buyScore++; else if(fh<sh) sellScore++;
   if(rsi>=52 && rsi<=68) buyScore++;
   if(rsi<=48 && rsi>=32) sellScore++;

   MqlRates bars[];
   ArraySetAsSeries(bars,true);
   if(CopyRates(symbol,PERIOD_M5,1,3,bars)==3)
   {
      if(bars[0].close>bars[0].open && bars[0].close>bars[1].close) buyScore++;
      if(bars[0].close<bars[0].open && bars[0].close<bars[1].close) sellScore++;
   }
   if(rsi>70 || rsi<30) { buyScore=MathMin(buyScore,2); sellScore=MathMin(sellScore,2); }

   int direction=0,score=0;
   if(buyScore>=MinimumSignalScore && buyScore>sellScore) { direction=1; score=buyScore; }
   if(sellScore>=MinimumSignalScore && sellScore>buyScore) { direction=-1; score=sellScore; }
   if(direction==0 || IsDuplicate(symbol,direction)) return;

   double entry=(direction>0 ? tick.ask : tick.bid);
   double zone=atr*0.15;
   double sl=(direction>0 ? entry-atr*1.5 : entry+atr*1.5);
   double tp1=(direction>0 ? entry+atr*1.5 : entry-atr*1.5);
   double tp2=(direction>0 ? entry+atr*2.5 : entry-atr*2.5);
   string side=(direction>0 ? "ALIM" : "SATIM");
   string msg="MT5 COKLU VARLIK SINYALI\n"+symbol+" | "+side+
      "\nGiris araligi: "+PriceText(symbol,entry-zone)+" - "+PriceText(symbol,entry+zone)+
      "\nStop: "+PriceText(symbol,sl)+
      "\nTP1: "+PriceText(symbol,tp1)+
      "\nTP2: "+PriceText(symbol,tp2)+
      "\nM5 RSI: "+DoubleToString(rsi,1)+" | Puan: "+IntegerToString(score)+
      "\nNot: Analiz sinyalidir; emir acilmaz. Spread ve haberleri kontrol et.";
   Print(msg);
   if(EnablePush)
   {
      string push="MT5 | "+symbol+" "+side+
         " | Giris "+PriceText(symbol,entry-zone)+"-"+PriceText(symbol,entry+zone)+
         " | SL "+PriceText(symbol,sl)+
         " | TP1 "+PriceText(symbol,tp1)+
         " | TP2 "+PriceText(symbol,tp2)+
         " | Puan "+IntegerToString(score);
      SendNotification(push); // Keep push text short for MT5 notification limits.
   }
   if(EnableTelegram) SendTelegram(msg);
   RememberSignal(symbol,direction);
}

void ScanAllSymbols()
{
   int total=SymbolsTotal(false);
   int limit=total;
   if(MaxSymbolsPerScan>0) limit=MathMin(total,MaxSymbolsPerScan);
   int scanned=0;
   for(int i=0;i<total && scanned<limit;i++)
   {
      string sym=SymbolName(i,false);
      if(sym=="") continue;
      AnalyzeSymbol(sym);
      scanned++;
   }
   Print("Tarama tamamlandi. Terminal sembol sayisi=",total,
         " | Bu tur taranan=",scanned,
         " | Limit=",(MaxSymbolsPerScan==0 ? "TUMU" : IntegerToString(MaxSymbolsPerScan)));
}

int OnInit()
{
   if(ScanEverySeconds<30 || MaxSymbolsPerScan<0)
      return INIT_PARAMETERS_INCORRECT;
   EventSetTimer(ScanEverySeconds);
   Print("MT5 Multi-Asset Signal Scanner basladi. Emir gonderme ozelligi yok.");
   ScanAllSymbols();
   return INIT_SUCCEEDED;
}

void OnTimer(){ ScanAllSymbols(); }
void OnDeinit(const int reason){ EventKillTimer(); }
