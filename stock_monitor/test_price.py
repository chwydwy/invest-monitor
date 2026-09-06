import FinanceDataReader as fdr
import pandas as pd

def check_fdr_columns():
    print("--- KRX 상장사 리스트 가져오는 중 ---")
    df = fdr.StockListing('KRX')
    
    # 1. 전체 컬럼명 출력
    print("\n[1. 전체 컬럼명 리스트]")
    print(df.columns.tolist())
    
    # 2. 삼성전자 데이터 샘플 출력
    print("\n[2. 삼성전자 데이터 샘플]")
    samp = df[df['Name'] == '삼성전자']
    if not samp.empty:
        print(samp.iloc[0])
        
        # 시가총액 관련 컬럼 추측 및 타입 확인
        # 보통 'Marcap', 'Amount', 'Stocks' 등이 후보입니다.
        for col in df.columns:
            if col in ['Marcap', 'Stocks', 'Shares', 'Amount']:
                val = samp[col].values[0]
                print(f"\n[3. 데이터 타입 확인] 컬럼: {col}, 값: {val}, 타입: {type(val)}")
    else:
        print("삼성전자를 찾을 수 없습니다.")

if __name__ == "__main__":
    check_fdr_columns()