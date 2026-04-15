import stdlib.sl
import libs/fn.sl
import fn_import_base.sl

fn func1(int x){
    let number y = x + 1;
    y = y / 2;
    return y;
}

fn main(){
    print(func1(5))
}
